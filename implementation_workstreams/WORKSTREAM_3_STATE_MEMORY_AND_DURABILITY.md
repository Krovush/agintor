# Workstream 3: State, Memory, and Durability

## Outcome

- Runtime-owned state becomes serializable, durable, restartable, and queryable from the workspace.
- Checkpoint envelopes from Workstream 2 acquire a real persistence layer backed by stable snapshot contracts.
- Short-term memory becomes durable provenance instead of an in-memory execution graph that disappears after the run.
- Long-term memory becomes a versioned durable graph with contradiction handling, retrieval diagnostics, and explicit write lineage.
- Recovery decisions become auditable through environment fingerprints, compatibility checks, and replay or diff tooling.

## Prerequisites

- Workstream 2 exit gates are complete.
- The runtime already emits stable checkpoint envelopes, branch contracts, side-effect receipts, and structured runtime events.
- The runtime host and export boundary are already fixed.

## Sequence Position

- This workstream starts only after Workstream 2 freezes checkpoint boundaries, branch semantics, side-effect receipts, and runtime event shapes.
- Workstream 4 depends on this layer for resumable search and inspectable evaluation lineage.
- Workstream 5 depends on this layer for durable promoted-tool records, async job recovery, and provider replay lineage.

## Boundaries

- Own the persistence contract for runtime-owned state, checkpoint storage, short-term provenance storage, long-term-memory durability, replay records, environment fingerprints, and recovery diagnostics.
- Own reconstruction of persisted state back into live runtime objects inside the bundled solve-time kernel.
- Keep scheduling policy, checkpoint timing, branch concurrency rules, per-tool sandbox behavior, provider feature semantics, export packaging, and factory-search persistence outside this workstream.
- Keep validation and test traces out of runtime durability surfaces except where immutable held-out reports explicitly reference runtime artifacts by ID.

## Non-Goals

- Remote state stores or managed databases
- Cross-runtime shared memory services
- GUI replay browsers
- Unbounded conversational memory outside the bounded runtime and task model

## Baseline

- `FixedShell` already centralizes runtime state and enforces critical invariants.
- `ShortTermGraph` already has the right semantic model: append-only structure, summaries with backlinks, and raw-output reachability rules.
- `LongTermGraph` already prioritizes exact symbols and exact paths over weaker similarity signals.
- Runtime isolation today still relies too heavily on `copy.deepcopy()` and process-local mutation rollback.
- `Checkpoint` is still a runtime object rather than a durable restore envelope.
- `EnvironmentFingerprint` exists as vocabulary but is not yet extracted from real runtime execution.
- The current codebase already has good invariant tests, but not yet a durable runtime-state model.

## Storage Decisions

- Keep the MVP storage strategy local, inspectable, and deterministic:
  - SQLite for indexes, lineage, and query surfaces
  - JSON checkpoint envelopes for portability and hashing
  - JSONL or structured rows for traces and replay
- Use the canonical runtime-state layout:

```text
workspace/
  state/
    runtime_state.sqlite
    checkpoints/
    traces/
    short_term/
    long_term/
    replays/
```

- Keep canonical JSON serialization deterministic and hashable.

## Core Decisions

- Use a local durable store for the MVP:
  - SQLite for indexed metadata and lineage
  - JSON checkpoint envelopes for full snapshots
  - JSONL or structured rows for traces and replay
- Treat persisted state as runtime-owned data. It must be restorable by the bundled runtime kernel without private host-package state.
- Replace ad hoc deep copies with explicit subsystem snapshot and restore methods.
- Preserve exact-first retrieval. Durable graph richness must not weaken the target spec's exact symbol and path precedence.
- Add a tiny deterministic `working_memory` block to runtime state for always-visible current-task facts, accepted constraints, and current-plan state. It must stay small and derived from validated runtime state rather than becoming an unbounded transcript cache.
- Keep secrets out of persisted state. Store digests, IDs, and redacted metadata only.

## Phase 1: Freeze Serializable Snapshot Contracts

- Add versioned snapshot models for:
  - `MessageBoardSnapshot`
  - `OpenHandleSnapshot`
  - `WorkingMemorySnapshot`
  - `ShortTermGraphSnapshot`
  - `LongTermGraphSnapshot`
  - `RuntimeBudgetSnapshot`
  - `BranchStateSnapshot`
  - `RuntimeStateSnapshot`
  - `CheckpointEnvelope`
  - `EnvironmentFingerprint`
  - `RecoveryRecord`
- Every snapshot contract must include:
  - `storage_schema_version`
  - `runtime_abi`
  - canonical ordering rules
  - validation logic
  - canonical JSON form for hashing and diffing
- Add explicit `snapshot()` and `restore()` support on:
  - message board
  - open-handle table
  - short-term graph
  - long-term graph
  - tool registry
  - predictor bank
  - `FixedShell`
  - `RuntimeState`
- Add round-trip tests that rebuild equivalent live objects and re-run shell invariant checks after restore.

## Phase 2: Build the Runtime State Store

- Add local persistence modules such as:
  - `agintor/state_store.py`
  - `agintor/checkpoint_store.py`
  - `agintor/replay_store.py`
- Persist runtime-owned state under a stable layout:

```text
workspace/
  state/
    runtime_state.sqlite
    checkpoints/
    traces/
    short_term/
    long_term/
    replays/
```

- Index at least:
  - run IDs
  - task IDs
  - episode IDs
  - checkpoint IDs
  - checkpoint lineage
  - branch IDs
  - handle and job IDs
  - side-effect receipts
  - artifact refs
  - replay refs
  - environment-fingerprint refs
  - recovery outcomes
- Store full checkpoint envelopes as JSON payloads so they remain inspectable and portable across local and Docker execution.

## Phase 3: Replace Deep-Copy Isolation with Subsystem Snapshots

- Remove direct deep-copy rollback from solve-time branch isolation.
- Make branch and worker isolation use subsystem-owned snapshots for:
  - tool registry state
  - predictors
  - open handles
  - working memory
  - long-term memory write staging
  - branch-local short-term state
- Require every runtime-owned subsystem that affects replay or restore to expose stable snapshot boundaries.
- Add rollback tests that prove branch-local mutations do not leak into parent runtime state unless published through explicit branch publications.

## Phase 4: Persist Short-Term Provenance and Replay Data

- Persist short-term runtime provenance for retained runs, including:
  - nodes
  - edges
  - hidden-node flags
  - summary backlinks
  - checkpoint publications
  - artifact lineage
  - handle and job lineage
  - verifier-evidence refs
  - run-level trace rows
  - side-effect receipt refs
- Add query APIs that answer:
  - which checkpoint produced artifact `X`
  - which summary hides raw node `Y`
  - which branch waited on handle or job `Z`
  - which verifier evidence supported a result
  - how a resumed run differs from its original lineage
- Add replay and diff tooling for:
  - normalized trace diff
  - short-term graph diff
  - checkpoint lineage diff
  - artifact lineage diff
  - receipt diff
- Keep replay and diff surfaces library-first and deterministic. Add CLI wrappers only after the APIs stabilize.

## Phase 5: Add Deterministic Working Memory

- Add a tiny always-visible `working_memory` block to runtime state snapshots.
- Restrict it to:
  - current objective
  - accepted constraints
  - active plan summary
  - verified facts
  - unresolved critical items
  - current branch or checkpoint references
- Populate it only from accepted checkpoints, verified evidence, and current runtime state.
- Do not allow `working_memory` to become a hidden transcript mirror or an uncontrolled context dump.
- Persist it with the rest of runtime state so resume behavior and replay diffs remain explainable.

## Phase 6: Turn Long-Term Memory into a Durable Versioned Graph

- Extend durable long-term memory with:
  - explicit edges
  - version timestamps
  - write lineage
  - contradiction markers
  - tombstones
  - verifier-support refs
  - source-task and source-checkpoint lineage
- Preserve the existing exact-first retrieval invariant:
  - exact symbol
  - exact path
  - lexical overlap
  - embedding similarity
  - neighbor expansion
- Add explicit write actions such as:
  - `new`
  - `merge_support`
  - `refine_version`
  - `record_conflict`
  - `tombstone`
- Persist retrieval diagnostics so a query result can explain whether it ranked highly because of:
  - exact symbol hit
  - exact path hit
  - lexical overlap
  - embedding similarity
  - neighbor expansion
  - verifier support
  - same-task affinity

## Phase 7: Capture Environment Fingerprints and Recovery Diagnostics

- Extract environment fingerprints from real execution facts, including:
  - runtime backend
  - runtime hash
  - kernel version
  - sandbox hash
  - dependency digest
  - provider identity
  - selected model class
  - filesystem policy
  - network policy
  - tool runtime info
- Link fingerprints to:
  - checkpoints
  - tool runs
  - provider calls
  - promoted-tool refs
  - artifacts
  - recovery attempts
- Define a resume compatibility matrix:
  - exact compatible: resume
  - partially compatible: resume with degraded handle or job recovery
  - incompatible: fail closed
- Persist typed recovery outcomes and diagnostics for every resume attempt.

## Regression Gates

- Add deterministic tests for:
  - full runtime-state round-trip
  - branch rollback without leakage
  - checkpoint recovery after forced stop
  - exact-first retrieval stability after reload
  - contradiction insertion and tombstoning
  - degraded versus clean resume classification
  - replay diff determinism
  - task and episode memory-scope resets with persisted state present
- Extend runtime tests so branch restore and recovery flows use the durable store, not in-memory shortcuts.

## Handoff to Workstream 4

- Workstream 4 receives:
  - a durable runtime-state store
  - replayable checkpoints and traces
  - queryable short-term provenance
  - versioned long-term memory
  - environment-aware recovery diagnostics
- Workstream 4 must use those durable artifacts as the basis for evaluation, held-out reports, and resumable search.

## Acceptance Gates

1. Full runtime state round-trips through persisted snapshots and passes shell invariants after restore.
2. Checkpoint envelopes persist to disk and can rebuild live runtime state after process death.
3. Branch isolation uses subsystem snapshots rather than raw deep copies.
4. Retained runs leave behind durable short-term provenance and replay artifacts that can be queried from the workspace.
5. Long-term memory survives restart when appropriate, resets at task or episode boundaries when required, and records contradictions and tombstones without losing provenance.
6. Working memory remains small, deterministic, and explainable across resume and replay.
7. Environment fingerprints and recovery diagnostics explain why a resume succeeded, degraded, or failed closed.

## File Ownership

- `agintor/shell.py`: runtime snapshot and restore lifecycle, invariant validation after restore
- `agintor/memory_graph.py`: durable short-term and long-term graph semantics, retrieval diagnostics, contradiction handling
- `agintor/runtime_api.py`: snapshot contracts and checkpoint-envelope schemas
- `agintor/runtime_sdk/`: runtime-owned restore, replay, and checkpoint validation entrypoints
- `agintor/runner.py`: integration with persistence hooks and recovery handoff
- `agintor/state_store.py`: indexed runtime metadata store
- `agintor/checkpoint_store.py`: checkpoint persistence and lookup
- `agintor/replay_store.py`: replay and diff persistence surfaces
- `tests/test_core.py`, `tests/test_runtime_spec.py`, and adjacent new persistence tests: round-trip, replay, diff, recovery, and memory regression coverage

## Deferred

- Distributed state stores
- External vector databases
- GUI replay explorers
- Cross-machine portability beyond the declared compatibility matrix
