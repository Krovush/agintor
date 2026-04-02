# Workstream 3: State, Memory, And Durability

## Outcome

- Runtime host state must become serializable, durable, and restartable. A stopped or crashed run must be recoverable from persisted runtime artifacts instead of relying on in-process memory.
- Short-term memory must become a persisted provenance system, not only an in-memory execution graph. Runs, checkpoints, handles, artifacts, and compaction lineage must be queryable after execution.
- Long-term memory must become a durable, versioned knowledge graph with explicit write history, contradiction handling, and retrieval diagnostics while preserving exact-symbol and exact-path priority rules.
- Observability must stay CLI-first and workspace-first. The MVP needs durable query surfaces and replay/diff tooling, not a bespoke GUI.

## Boundaries

- Own the persistence contract for solve-time runtime state, checkpoint payloads, replay/provenance storage, short-term graph durability, and long-term memory durability inside the runtime boundary.
- Own reconstruction of persisted state back into live `FixedShell`, `RuntimeState`, and related runtime objects after interruption.
- Keep runtime scheduling, checkpoint emission timing, branch semantics, and request execution flow outside this workstream. This workstream owns the stored state model and recovery path, not the scheduler policy or transport layer.
- Keep export bundling and CLI packaging outside this workstream. This workstream owns durable state formats and asset boundaries, not the user-facing export contract.
- Keep tool-runtime internals, async job lifecycle policy, sandbox implementation, and provider/runtime environment semantics outside this workstream. This workstream may persist their state and fingerprints, but it does not own those subsystems' behavioral logic.
- Keep factory archive persistence, evaluation restart, and long-running search orchestration outside this workstream. This workstream is runtime-state durability, not factory-search durability.

## Non-Goals

- Do not start with process-image checkpointing, distributed state stores, or cluster orchestration.
- Do not build a web UI before the persisted state model, query API, and replay artifacts are stable.
- Do not block durability work on a sophisticated embedding stack or external vector database. The current cheap embedding baseline can remain until the durable graph and retrieval diagnostics are correct.
- Do not move factory state, validation/test traces, or mutation history into the runtime durability layer.

## Baseline

- `agintor/shell.py` centralizes runtime-owned state in `FixedShell`: short-term memory, long-term memory, message board, open-handle table, predictors, agent pool, sandbox manager, tool registry, tool executor, and trace writing.
- `FixedShell.reset_for_task()` enforces the intended long-term memory boundary: memory resets per task unless transfer-scored execution keeps the same episode scope.
- `FixedShell.validate_invariants()` enforces open-handle integrity, short-term raw-output reachability, and long-term memory leakage rules.
- `agintor/memory_graph.py` provides the core semantics for short-term memory: append-only nodes and edges, summary replacement with backlinks, and hidden-node reachability validation.
- `LongTermGraph` supports the required node types and ranks retrieval with exact symbol and exact path matches ahead of lexical and embedding-style similarity.
- `agintor/runner.py` emits checkpoint summaries, promotes long-term memory candidates, records short-term graph artifacts and handles, and writes traces when `retain_artifacts=True`.
- The durability model is process-local:
  `runner._execute_isolated_frame()` deep-copies tool registry state, open handles, long-term memory, and predictor state for isolation, then restores them in memory.
- `ShortTermGraph` and `LongTermGraph` expose `to_jsonable()` or raw node access but do not yet provide a first-class durable storage contract or reload path.
- `Checkpoint` is a summary-oriented runtime object. It is not a restartable persisted state envelope.
- `EnvironmentFingerprint` exists in schema vocabulary but is not yet extracted from real runtime execution.
- The existing tests cover important baseline invariants: short-term backlinks, exact-first retrieval, open-handle validation, long-term memory scope resets, and checkpoint contract shape.
- Runtime durability depends on a solve-time kernel that lives in the Agintor package. Persisted state is not defined as a runtime-owned contract that can be restored on a fresh machine from durable artifacts alone.

## Storage Decisions

- Use a local, workspace-backed durability model for the MVP.
- Store indexed runtime metadata in a local SQLite database and keep full checkpoint or replay payloads as JSON artifacts on disk. SQLite gives queryability and crash-safe commits without introducing a service dependency; JSON keeps snapshots inspectable and diffable.
- Treat persisted runtime state as runtime-owned data, not host-owned process leftovers. Checkpoints and replay artifacts must be restorable by the solve-time runtime kernel without requiring private host-package implementation state.
- Make shell subsystems expose explicit snapshot and restore methods. Stop relying on raw dict mutation and ad hoc `copy.deepcopy()` of internal fields as the persistence boundary.
- Keep short-term provenance append-only in storage just as it is in memory. Compaction may hide raw nodes, but persisted backlinks must preserve reachability.
- Add explicit long-term graph edge and version history support instead of treating the durable knowledge base as only a node map.
- Keep secrets out of persisted runtime state. Environment fingerprints may record provider names, model classes, sandbox hashes, dependency digests, and backend facts, but not live credentials or raw secret values.

## Phase 1: Freeze Serializable Runtime-State Contracts

- Add serializable models for:
  `MessageBoardSnapshot`,
  `OpenHandleSnapshot`,
  `ShortTermGraphSnapshot`,
  `LongTermGraphSnapshot`,
  `RuntimeStateSnapshot`,
  `RuntimeBudgetSnapshot`,
  `CheckpointEnvelope`,
  and any required frame or branch snapshot objects.
- Add snapshot and restore methods on:
  `MessageBoard`,
  `OpenHandleTable`,
  `ShortTermGraph`,
  `LongTermGraph`,
  `FixedShell`,
  and `RuntimeState`.
- Preserve deterministic ordering in all serialized collections that affect replay or hashing.
- Define versioned storage schemas up front so future schema revisions can reject invalid checkpoint data cleanly instead of loading partially.
- Add round-trip tests proving that serialized state can rebuild equivalent live objects without violating shell invariants.

`Exit gate:` full runtime host state can be snapshotted, serialized, restored, and validated without relying on private mutable internals.

## Phase 2: Add The Runtime State Store And Checkpoint Manager

- Add a runtime-state storage module, for example:
  `agintor/state_store.py`
  and `agintor/checkpoint_store.py`.
- Persist runtime-owned state under a stable workspace layout such as:

```text
workspace/
  state/
    runtime_state.sqlite
    checkpoints/
    traces/
    short_term/
    long_term/
```

- Store run IDs, task IDs, episode IDs, checkpoint IDs, parent-child checkpoint lineage, branch IDs, handle IDs, and snapshot file locations in SQLite.
- Store full checkpoint envelopes as JSON payloads so they stay inspectable and portable across local and Docker execution.
- Keep checkpoint and replay formats portable across runtime execution environments. The runtime kernel must be able to validate and restore them without importing factory-only code.
- Expose recovery APIs that can:
  load the latest checkpoint for a run,
  load a specific checkpoint by ID,
  rebuild `FixedShell`,
  rebuild `RuntimeState`,
  and hand control back to the runner.
- Keep checkpoint emission boundaries fixed, but make the storage and recovery path complete enough that those checkpoints become restartable rather than summary-only.

`Exit gate:` a runtime run can persist restartable checkpoints to disk and reload them after process death without manual reconstruction.

## Phase 3: Replace In-Memory Isolation Snapshots With Subsystem Snapshot APIs

- Remove the direct deep-copy isolation pattern in `runner._execute_isolated_frame()` for:
  tool registry internals,
  open handles,
  long-term memory state,
  and predictor state.
- Replace it with explicit snapshot and restore contracts owned by the corresponding shell subsystems.
- Ensure branch or worker isolation does not depend on mutating raw subsystem dictionaries in place.
- Require predictor and tool-registry subsystems to offer storage-safe snapshot handles so runtime durability does not depend on their private layout.
- Add tests for isolated execution rollback that prove task-local branches cannot leak mutated runtime state back into the parent shell unless publication is explicit.

`Exit gate:` isolated worker execution uses stable snapshot and restore interfaces, not ad hoc deep copies of shell internals.

## Phase 4: Persist Short-Term Provenance And Replay Data

- Persist short-term graph nodes, edges, hidden-node sets, checkpoint publications, artifact refs, open-handle refs, and run-level trace rows for every retained run.
- Add a query API that can answer:
  which checkpoint produced an artifact,
  which summary hides a raw node,
  which handle a branch was waiting on,
  which verifier evidence was attached to a result,
  and how a resumed run relates to its original lineage.
- Add normalized replay and trace-diff tooling so two runs can be compared on:
  trace rows,
  short-term graph structure,
  checkpoint lineage,
  artifact lineage,
  and handle state transitions.
- Keep the first observability surface bounded:
  CLI-readable JSON outputs,
  workspace reports,
  and library query APIs.
  A graphical explorer can come later if the stored model proves stable.
- Make retained replay data explicit and opt-in where appropriate so routine evaluation can stay lightweight.

`Exit gate:` retained runs can be queried and diffed from persisted provenance artifacts without re-executing the task.

## Phase 5: Persist And Version The Long-Term Memory Graph

- Extend `LongTermGraph` from a durable node map into a durable graph with explicit edges, write lineage, version timestamps, and contradiction markers.
- Add contradiction-aware memory actions:
  merging supporting evidence,
  refining a node version,
  recording conflicting claims,
  and tombstoning stale or invalid entries without losing provenance.
- Preserve the current retrieval invariant that exact symbol and exact path matches dominate. Durable graph richness must not weaken that rule.
- Persist retrieval diagnostics alongside query results so the system can explain why a node ranked highly:
  exact symbol hit,
  exact path hit,
  lexical overlap,
  embedding similarity,
  neighbor expansion,
  verifier support,
  or same-task affinity.
- Keep the cheap embedding baseline initially, but make the retrieval explanation durable so later embedding upgrades are measurable rather than opaque.
- Add tests for:
  durable upsert and tombstone behavior,
  contradiction insertion,
  retrieval ranking stability after reload,
  and episode/task memory-scope resets with persisted state present.

`Exit gate:` long-term memory survives process restart, preserves versioned knowledge history, and is inspectable and scope-correct across task and episode boundaries.

## Phase 6: Capture Environment Fingerprints And Recovery Diagnostics

- Extract `EnvironmentFingerprint` nodes from real execution facts:
  sandbox hash,
  runtime backend,
  tool runtime,
  dependency digest,
  selected model class,
  provider identity,
  filesystem mount policy,
  and other non-secret execution facts exposed by runtime execution.
- Store environment fingerprints in long-term memory and link them to tool runs, failures, promoted procedures, and artifact signatures where relevant.
- Use persisted environment fingerprints during recovery to detect invalid resume attempts, such as a missing sandbox artifact, changed backend, or unavailable tool runtime.
- Record recovery outcomes explicitly:
  resumed,
  resumed with degraded handle recovery,
  failed closed because state validation failed,
  or failed closed because required runtime assets were missing.

`Exit gate:` runtime durability includes enough environment and recovery metadata to explain why a resume succeeded, degraded, or failed closed.

## MVP Acceptance Sequence

1. `FixedShell`, `RuntimeState`, short-term memory, long-term memory, message-board state, and open-handle state can all round-trip through persisted snapshots and satisfy existing invariants.
2. Persisted runtime state can be restored by the bundled solve-time runtime kernel without depending on private host-package implementation state.
3. A runtime interrupted after checkpoint emission can reload from the persisted checkpoint, rebuild live runtime state, and continue or fail closed with a precise recovery reason.
4. Retained runs leave behind durable provenance artifacts that support replay queries and normalized trace or graph diffs from the workspace.
5. Long-term memory persists across process restart when it should, resets when task or episode boundaries require it, and records contradiction or tombstone history without losing provenance.
6. Environment fingerprints are captured from real execution and are available in persisted recovery and replay records without leaking secrets.

## File Ownership

- `agintor/shell.py`: shell snapshot and restore lifecycle, task or episode memory-scope enforcement, invariant validation across restored state.
- `agintor/memory_graph.py`: short-term and long-term graph snapshot contracts, persisted graph semantics, contradiction edges, retrieval diagnostics, version history.
- `agintor/runtime_api.py`: serializable runtime-state, budget, frame, and checkpoint-envelope contracts.
- `agintor/runtime_sdk/` or equivalent bundled solve-time kernel package: runtime-owned restore and replay entrypoints plus checkpoint validation logic.
- `agintor/runner.py`: integration with checkpoint persistence, recovery hooks, replay publication, and subsystem snapshot use.
- `agintor/state_store.py`, `agintor/checkpoint_store.py`, `agintor/replay_store.py`:
  local persistence, indexing, recovery lookup, and query APIs.
- `tests/test_core.py`, `tests/test_algorithms.py`, and targeted new durability tests:
  round-trip serialization, restart recovery, scope resets, contradiction handling, replay queries, and trace-diff behavior.
