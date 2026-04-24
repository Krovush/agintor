# WS3 — State, Memory, and Durability: Implementation Plan

## Context

Workstream 3 adds durability, indexing, reconstruction, and query surfaces
around the existing WS1 export contract and WS2 runtime/resume semantics. It
does **not** redefine solve-time behavior. The authoritative design of record
is [WORKSTREAM_3_STATE_MEMORY_AND_DURABILITY.md](../../Desktop/Agintor%20MVP/implementation_workstreams/WORKSTREAM_3_STATE_MEMORY_AND_DURABILITY.md);
this file sequences that design against the current code state, pins the
critical file sites, and calls out hidden couplings a naïve
phase-by-phase read of the doc will miss.

The outcome WS3 must land:

- `CheckpointEnvelope` becomes `agintor.checkpoint-envelope.v4` with typed
  `working_state`, `trace_cursor`, plus extended subsystem snapshots.
- `RunStore` remains the only checkpoint authority; a new `state_store.py`
  adds an indexed SQLite query surface whose rows are always rebuildable
  from canonical JSON.
- `FixedShell.fork_branch()` stops using `copy.deepcopy` on live shell
  subsystems and uses a `snapshot()` / `restore()` / `fork_from_snapshot()`
  protocol instead.
- Long-term memory gains durable edges, write lineage, contradictions,
  tombstones, and retrieval diagnostics — exact-first retrieval preserved
  after reload.
- Recovery, environment fingerprints, working memory become typed records.
- `openai_api_traces/` is restructured into session-scoped canonical calls
  with rebuildable grouped views; repeated invocations no longer overwrite
  prior traces.

## Hidden couplings (read before starting)

The doc's phase ordering is correct but understates three coupling risks
that will cause rework if ignored:

1. **The v4 envelope bump must carry *extended* subsystem snapshot shapes,
   not just `WorkingMemorySnapshot` + `TraceCursorSnapshot`.** Phase 3's
   subsystem protocol and Phase 4's long-term write/edge lineage both need
   `LongTermGraphSnapshot` to grow edge, write-log, and diagnostic-ref
   fields. Ship the extended field shapes (default-empty where Phase 4
   fills them later) in the **same** v4 break as Phase 1 typed records.
   Otherwise Phase 4 forces a second envelope bump.
2. **`ShellStateSnapshot` gains a `predictor_snapshot` field and
   `OpenHandleTableSnapshot` in Phase 3.** Today
   [agintor/shell.py:485-487](../../Desktop/Agintor%20MVP/agintor/shell.py)
   carries predictor state across `fork_branch` via `copy.deepcopy` of
   private dicts, and `ShellStateSnapshot` stores `open_handles` as a raw
   `List[AsyncHandle]`. Dropping deep-copy without adding those snapshot
   fields silently drops predictor state on branch fork.
3. **Container path-rewrite narrowing: pin first, narrow second.**
   [agintor/container_runtime.py:1082-1094](../../Desktop/Agintor%20MVP/agintor/container_runtime.py)
   recursively rewrites `plan_snapshot`, `task_payload`,
   `runtime_state_snapshot`, and `side_effect_ledger` when a request-file
   reverse map exists. Phase 0 must pin the **current** durable-field set
   with positive tests before Phase 1 narrows the recursion so opaque
   nested payloads (`side_effect_ledger.receipts[*].result_ref`,
   `working_state`, `trace_cursor`) remain untouched.

## Phase 0 — Normalize the inherited baseline

Small, mandatory. Lands before any new durability layer.

- **Fix restore-time checkpoint identity.** In
  [agintor/task_runtime/checkpointing.py:123-125](../../Desktop/Agintor%20MVP/agintor/task_runtime/checkpointing.py)
  remove the `self.shell.latest_checkpoint_ref(...)` override; preserve the
  exact ref selected for restore. Public import path
  `agintor.runner.TaskRuntime` stays stable.
- **Fix `FixedShell.save_trace()` filename collisions.** At
  [agintor/shell.py:182](../../Desktop/Agintor%20MVP/agintor/shell.py) the
  current pattern is `{attempt_id}.{task_id}_{seed}.json`. Use the form
  `{attempt_id}.{task_id}_{seed}_step{episode_step_index}_{counter}.json`
  where `counter` is an attempt-scoped monotonic integer. This propagates
  into grouped-trace rebuild semantics in Phase 6, so pick the shape now.
- **Pin the current container path-rewrite boundary.** Extend
  [tests/test_container_runtime.py](../../Desktop/Agintor%20MVP/tests/test_container_runtime.py)
  with positive assertions that `run_root`, `source_checkpoint_ref`,
  `runtime_state_snapshot.latest_checkpoint_ref`, `attempt_snapshot.run_root`,
  `attempt_snapshot.resumed_from_checkpoint_ref`, async-handle paths, and
  branch-resume paths rewrite correctly today, **plus** negative
  assertions that `side_effect_ledger.receipts[*].result_ref`,
  `working_state_summary`/`working_state`, and `trace_cursor` remain
  byte-for-byte under request-file reverse mapping. Phase 1 narrows the
  recursion after this gate is green.
- **Pin grouped-execution identity round-trip.** Extend
  [tests/test_runtime_host.py](../../Desktop/Agintor%20MVP/tests/test_runtime_host.py)
  to cover `request_id`, `evaluation_unit_id`, `episode_kind`,
  `episode_step_index` survival through `RunManifest`,
  `ExecutionUnitRequestEnvelope`, and resume rebind.

**Exit gate:** Phase 0 tests green; no new durability code landed.

## Phase 1 — Typed persistence contracts + v4 envelope

All new public records are Pydantic models in
[agintor/schemas.py](../../Desktop/Agintor%20MVP/agintor/schemas.py),
rejecting unknown fields. Timestamps are epoch seconds via `now_ts()`.
Cross-record refs are string IDs or scoped canonical refs. None duplicate
`runtime_abi` / `kernel_version` / `storage_schema_version`.

New contracts (full field sets are frozen in the WS3 doc §"Phase 1"):

- `WorkingMemorySnapshot` with `VerifiedFactRef`
- `TraceCursorSnapshot`
- `EnvironmentFingerprint`
- `RecoveryAttempt` with `FingerprintDelta`
- `LongTermWriteRecord`
- `LongTermEdgeRecord`
- `LongTermEdgeType` enum (do **not** widen short-term `EdgeType` at
  [agintor/schemas.py:521-529](../../Desktop/Agintor%20MVP/agintor/schemas.py))
- `RetrievalDiagnosticRecord` with `RetrievalSignalRow`

**Envelope bump.** Bump `CheckpointEnvelope.checkpoint_schema_version` from
`agintor.checkpoint-envelope.v3` → `agintor.checkpoint-envelope.v4`. Replace
`working_state_summary: Dict[str, Any]` → `working_state: WorkingMemorySnapshot`
and `trace_cursor: Dict[str, Any]` → `trace_cursor: TraceCursorSnapshot` at
[agintor/schemas.py:1928-1957](../../Desktop/Agintor%20MVP/agintor/schemas.py).
Update the restore identity gate in
[agintor/task_runtime/checkpointing.py](../../Desktop/Agintor%20MVP/agintor/task_runtime/checkpointing.py)
in the same change so a v3 envelope cannot load into a v4 runtime. Update
the construction site at
[agintor/task_runtime/checkpointing.py:372-391](../../Desktop/Agintor%20MVP/agintor/task_runtime/checkpointing.py)
to produce typed records instead of inline dicts.

**Ship extended snapshot shapes in the same bump (coupling risk).** Extend
`ShellStateSnapshot` with `predictor_snapshot` and an
`OpenHandleTableSnapshot`. Extend `LongTermGraphSnapshot` with empty
edge-list, write-log-ref, and diagnostic-ref fields that Phase 4
populates. Extend `ShortTermGraphSnapshot` with typed summary-backlinks
and artifact-lineage fields for Phase 4. New fields default-empty; no
second v4.1 break needed.

**Narrow container path rewriting.** After Phase 0 pins the boundary, drop
the recursive `_rewrite_exact_string_payload` calls on `working_state`,
`trace_cursor`, and `side_effect_ledger.receipts[*].result_ref` at
[agintor/container_runtime.py:1082-1094](../../Desktop/Agintor%20MVP/agintor/container_runtime.py).
Keep rewriting for explicit durable-path fields only.

**Tests.** Round-trip every new contract; reject unknown/extra fields;
v3-envelope-in-v4-runtime negative test; Phase 0 path-rewrite suite stays
green with the narrowed recursion.

## Phase 2 — Indexed state layer (`agintor/state_store.py`)

**This is the largest phase — ~1500-2500 LOC plus the rebuild harness.**
Flag to the operator if budget is tight.

New public module
[agintor/state_store.py](../../Desktop/Agintor%20MVP/agintor/state_store.py).
Integrated into `RunStore` lifecycle — `RunStore` remains the sole run-root
writer and calls `state_store` write hooks only **after** each canonical
JSON write succeeds.

**Layout extension** under every run root:

```
runs/<run_id>/state/
  runtime_state.sqlite
  short_term/        # canonical JSONL shards per checkpoint
  long_term/writes/  # LongTermWriteRecord shards
  long_term/edges/   # LongTermEdgeRecord shards
  long_term/retrieval/  # RetrievalDiagnosticRecord shards
  recovery/
  recovery/fingerprints/
  working_memory/    # only when materialized outside the envelope
```

**SQLite configuration.** WAL journaling, `synchronous=NORMAL`,
`foreign_keys=ON`, `busy_timeout=5000ms`. Short-lived or thread-local
connections; never shared across threads. Declare
`STATE_STORE_SCHEMA_VERSION` with forward-only migrations; opening a
newer store with older code fails closed.

**What to index** (see doc §"What the indexed layer must index" for the
full list): runs, requests, evaluation units, tasks, episodes, attempts,
checkpoints and lineage, branches, receipts, artifacts, runtime events,
short-term nodes/edges, long-term writes/edges/retrieval, recovery
attempts, fingerprints, trace call IDs, trace-group references.

**Every row carries `canonical_ref` + `canonical_record_id`** back to the
owning JSON artifact. Run-local refs are run-root-relative; session-trace
refs are trace-session-relative and explicitly marked session-scoped.

**`rebuild_from_canonical(run_root)`** is a required entrypoint that drops
and reconstructs every index row from canonical JSON. It must produce
logically equivalent query results after deletion of
`state/runtime_state.sqlite` — this is the canonical recovery path and is
the biggest new test harness WS3 needs (budget half a day to a day just
for the fixture).

**Query surfaces.** Library-first APIs for the eight endpoints in the WS3
doc §"Required query surfaces".

**Scaffold order:** tables/migrations + index-only-stable-rows first
(runs, attempts, checkpoints, events, receipts, trace refs). Long-term,
recovery, fingerprint, and retrieval-diagnostic tables can land
schema-first with empty writers; Phases 4/5 fill them.

## Phase 3 — Subsystem snapshot protocol (replace deep-copy)

Target:
[agintor/shell.py:469-495](../../Desktop/Agintor%20MVP/agintor/shell.py).
Remove every `copy.deepcopy` of live shell-owned subsystems from
`FixedShell.fork_branch()`.

**Frozen protocol** on every branch-visible subsystem:

- `snapshot(self) -> SubsystemSnapshotRecord` — returns a frozen,
  serializable record (Pydantic model preferred). No references to
  mutable live objects.
- `restore(self, snapshot) -> None` — rebuilds live state from the
  record; may copy from immutable payload.
- `fork_from_snapshot(cls, snapshot) -> Self` — classmethod factory; the
  **only** path `fork_branch()` uses.

**Subsystems covered** (six):

- `ShortTermGraph`
- `LongTermGraph`
- `MessageBoard`
- `OpenHandleTable` (new `OpenHandleTableSnapshot`)
- `TaskLocalToolRegistry`
- Predictor bank (new `PredictorSnapshot` — today deep-copied via
  `_observations`/`_models`/`_ranking_weights` dicts)

Slot each `SubsystemSnapshotRecord` into `ShellStateSnapshot` and
`BranchResumeSnapshot` as a typed field, so checkpoint publish, restore,
and branch resume share the same serialization path.

`fork_branch()` becomes: call `snapshot()` once per parent subsystem,
pass records to child `fork_from_snapshot()`. Retain intentional shared
references (`event_dir`, `_runtime_event_lock`, `_runtime_event_state`,
scope IDs) — those are not branch-visible mutable state.

**Tests.**
New `tests/test_subsystem_snapshots.py` with a round-trip fixture for
each of the six subsystems, plus a branch-isolation test that mutates
child-shell subsystems and asserts parent shell is byte-identical after
branch teardown, plus branch-resume checkpoint round-trip.

## Phase 4 — Persist provenance and long-term lineage

May start after Phase 2 schema and Phase 3 snapshot shape land.

**Short-term provenance.** Emit at checkpoint / branch publication /
attempt finalization / explicit retention boundaries — **not** on every
graph mutation. Persist nodes, edges, hidden-node flags, summary
backlinks, artifact lineage, branch-publication lineage, open-handle /
async-job lineage, verifier-evidence refs, receipt refs, event refs.

**Long-term memory rewrite.** Expand
[agintor/memory_graph.py](../../Desktop/Agintor%20MVP/agintor/memory_graph.py)
from the current 197 LOC (flat `upsert`/`tombstone` only) into a durable
versioned graph with edges, `LongTermWriteRecord` emission,
`LongTermEdgeRecord` emission, contradiction detection (`action="conflict"`),
tombstone lineage, verifier-support refs, source-task/checkpoint lineage,
and `RetrievalDiagnosticRecord` emission on every retrieval.

**Write vocabulary.** Use the runtime's existing `upsert` / `merge` /
`refine` / `tombstone` vocabulary. Do **not** invent a second durability
language (no `merge_support`, no `refine_version`). Add `conflict` only
when a contradiction is detected.

**Retrieval diagnostics.** Persist every signal the retriever used:
`exact_file_path_hit`, `exact_symbol_hit`, `node_id_match`,
`verifier_support_score`, `lexical_overlap_score`,
`embedding_similarity_score`, `same_task_affinity_score`,
`synthesized_neighbor_expansion`, plus `exact_first_preserved`.

**Exact-first after reload.** The retrieval path today lives at
[agintor/memory_graph.py:163-197](../../Desktop/Agintor%20MVP/agintor/memory_graph.py)
and gives exact symbol/file-path matches top rank. That dominance must
survive reload — add a test that snapshots a populated graph, restores it
via the new protocol, and asserts retrieval order is identical plus
`exact_first_preserved=True` on every diagnostic.

## Phase 5 — Working memory, fingerprints, recovery ledger

May run in parallel with Phase 4 once Phase 3 lands.

**`WorkingMemorySnapshot`.** Derived only from accepted runtime state and
verified evidence — deliberately small. Construction lives beside
`_save_checkpoint_envelope` in
[agintor/task_runtime/checkpointing.py](../../Desktop/Agintor%20MVP/agintor/task_runtime/checkpointing.py).
Do **not** let it become a transcript mirror.

**`EnvironmentFingerprint` extraction.** Draw from runtime facts
scattered across `runtime_api.py`, `runtime_host.py`, and the capability
exchange in [agintor/runtime_host.py](../../Desktop/Agintor%20MVP/agintor/runtime_host.py)
(`runtime_backend`, `runtime_hash`, `runtime_abi`, `storage_schema_version`,
`kernel_version`, `runtime_isolation_policy`, `supported_guarantees`,
provider identity, model class, sandbox hash, tool-runtime IDs,
dependency digest, filesystem/network policy). Persist once per
fingerprint change keyed by `fingerprint_id` (hash of content fields).
Queryable both as a lineage record and as a `LongTermNodeType.EnvironmentFingerprint`
memory node.

**`RecoveryAttempt` ledger.** Record each resume reconciliation outcome
with `compatibility_result ∈ {"exact_compatible", "degraded_compatible",
"fail_closed"}`, fingerprint deltas that mattered, receipts reused /
reissued / blocked / invalidated, blocked node IDs, degraded plan node
IDs, resume explanation. WS3 **records** the outcome; it does not
redefine the reconciliation rules (WS2 semantics).

## Phase 6 — Trace-store topology (`agintor/openai_trace.py`)

Restructure the current flat `openai_api_traces/auto/calls/` store at
[agintor/openai_trace.py:622-634](../../Desktop/Agintor%20MVP/agintor/openai_trace.py)
into the session-scoped layout:

```
openai_api_traces/
  sessions/<session_id>/
    calls/*.json                      # canonical raw-call JSON
    materialization_state.json        # typed session manifest
    INDEX.md, TRANSCRIPT.md           # derived
    builds/<build_id>/                # only when build_id present
    solves/<request_id>/              # request_id always required
    runtime_tasks/<task_id>/seed_<seed>/runtimes/<runtime_hash>/requests/<request_or_evaluation_unit_id>/
```

**Trace-context resolution helper.** Single helper in `openai_trace.py`
— all call sites and grouped rebuild use it.

- `session_id` **required**. If absent on incoming `OpenAITraceContext`,
  derive once per host process from
  `(host_session_start_time, host_pid, host_machine_id_hash)`, cache it,
  and write it back into the persisted call record's `trace_context`
  and `TraceCursorSnapshot.last_session_id`.
- `build_id` optional — skip `builds/` materialization when absent, no
  placeholder directories.
- `request_id` **always present** for solve-time calls (WS1/WS2 contract).
  Absence is a bug.
- Runtime-task materialization **only** when all of `task_id`, `seed`,
  `runtime_hash`, and `request_id`-or-`evaluation_unit_id` are present.
  Canonical key: `task_id|seed|runtime_hash|request_or_evaluation_unit_id`.
  Skip silently if any is missing — no substitute keys, no `_unscoped/`.

**Canonical raw-call fields** (top-level, first-class): request payload,
request metadata, full `OpenAITraceContext` with resolved `session_id`,
provider role, raw response envelope, usage, latency, error, canonical
call ID, ordering information (so duplicate invocations never collide).

**`materialization_state.json`** is the authoritative session-scoped
manifest with `session_id`, `schema_version`, `last_finalized_call_id`,
`known_call_ids`, materialized/pending lists for builds, solves, runtime
tasks, `errors`, `updated_at`. `INDEX.md` / `TRANSCRIPT.md` are derived.

**Rebuild API.** Regenerate grouped views after interruption from
canonical raw-call JSON alone — no provider calls reissued. Grouped
transcripts go through one shared renderer in `openai_trace.py`. WS5
owns per-call render fidelity later; WS3 owns topology.

## Critical files

| File | WS3 role |
|---|---|
| [agintor/schemas.py](../../Desktop/Agintor%20MVP/agintor/schemas.py) | New typed contracts, v4 envelope bump, extended `ShellStateSnapshot`/`ShortTermGraphSnapshot`/`LongTermGraphSnapshot` |
| [agintor/run_store.py](../../Desktop/Agintor%20MVP/agintor/run_store.py) | `state/` subtree creation, state_store write-hook integration |
| [agintor/state_store.py](../../Desktop/Agintor%20MVP/agintor/state_store.py) | **New.** SQLite index + migrations + `rebuild_from_canonical` |
| [agintor/shell.py](../../Desktop/Agintor%20MVP/agintor/shell.py) | `fork_branch` rewrite, `save_trace` collision fix, subsystem snapshot hooks |
| [agintor/memory_graph.py](../../Desktop/Agintor%20MVP/agintor/memory_graph.py) | Durable edges, write lineage, contradictions, retrieval diagnostics, exact-first preservation |
| [agintor/task_runtime/checkpointing.py](../../Desktop/Agintor%20MVP/agintor/task_runtime/checkpointing.py) | Line-123 checkpoint-ref fix, v4 identity gate, typed record production, `RecoveryAttempt` recording |
| [agintor/task_runtime/branching.py](../../Desktop/Agintor%20MVP/agintor/task_runtime/branching.py) + [branch_execution.py](../../Desktop/Agintor%20MVP/agintor/task_runtime/branch_execution.py) | Branch resume snapshot via new subsystem protocol |
| [agintor/container_runtime.py](../../Desktop/Agintor%20MVP/agintor/container_runtime.py) | Narrowed path rewriting — durable fields only |
| [agintor/openai_trace.py](../../Desktop/Agintor%20MVP/agintor/openai_trace.py) | Session-scoped topology, materialization manifest, grouped rebuild, shared renderer |
| [agintor/runtime_api.py](../../Desktop/Agintor%20MVP/agintor/runtime_api.py) | Resume rebind for v4 typed fields |
| [agintor/runtime_sdk/bundle.py](../../Desktop/Agintor%20MVP/agintor/runtime_sdk/bundle.py) | Vendor `state_store.py` if exported runtimes need it |

## Verification

**Cheap (extend existing test files):**

- Checkpoint-ref preservation across explicit-ref / run-ref /
  external-store resume → `tests/test_runtime_execution.py`
- Non-colliding trace filename under repeated invocations →
  `tests/test_runtime_execution.py`
- Grouped-execution identity round-trip → `tests/test_runtime_host.py`
- Path-rewrite boundary (positive + negative) →
  `tests/test_container_runtime.py`
- v3-envelope-into-v4-runtime fail-closed → `tests/test_runtime_execution.py`
- Unknown-field rejection + round-trip for every new typed contract →
  new `tests/test_persistence_contracts.py`

**New harnesses required (budget each):**

- `tests/test_subsystem_snapshots.py` — six subsystem round-trips plus
  branch-isolation non-leak. ~0.5 day.
- `tests/test_state_store.py` with `rebuild_from_canonical` fixture —
  construct a run with every indexed record populated, delete
  `state/runtime_state.sqlite`, rebuild, compare query results. **~1 day;
  largest new scaffold.**
- `tests/test_memory_persistence.py` — long-term write lineage,
  contradictions/tombstones, retrieval diagnostics, exact-first after
  reload. ~0.5 day.
- `tests/test_recovery_ledger.py` — exact / degraded / fail-closed
  outcomes driven by fingerprint deltas and receipt reconciliation.
  ~0.5 day.
- `tests/test_trace_store.py` — grouped rebuild identity, missing
  `build_id` / missing runtime-task identity, `session_id` derivation,
  `materialization_state.json` round-trip, duplicate-invocation
  non-overwrite. ~0.5 day.

**Manual end-to-end check:**

```bash
pip install -e ".[dev]"
pytest                                       # offline fast markers
pytest -m integration                        # runtime-backed offline
pytest tests/test_state_store.py             # index rebuild round-trip
pytest tests/test_subsystem_snapshots.py     # branch isolation protocol
pytest tests/test_container_runtime.py       # path-rewrite boundary
agintor init-runtime /tmp/ws3_rt
agintor solve /tmp/ws3_rt demo_task --suite demo    # produces run root
ls /tmp/ws3_rt/workspace/runs/*/state/              # verify state subtree
rm /tmp/ws3_rt/workspace/runs/*/state/runtime_state.sqlite
python -c "from agintor.state_store import rebuild_from_canonical; rebuild_from_canonical('...')"
# query APIs should return equivalent rows
```

## Sizing

Rough engineering-day estimate assuming single implementer, reusing
existing patterns:

- Phase 0: 0.5-1 day
- Phase 1: 1-2 days
- Phase 2: **3-5 days** (largest phase; flag to operator)
- Phase 3: 2-3 days
- Phase 4: **3-5 days** (effective rewrite of `memory_graph.py`)
- Phase 5: 2-3 days
- Phase 6: 2-4 days

Total 14-23 engineering days sequential; ~9-14 wall-clock days if
Phases 4/5/6 run in parallel after Phase 3 lands.

## Out of scope

Same as doc §"Non-Goals" and §"Deferred": no remote state stores, no
parallel checkpoint authority, no WS5 provider-capture richness, no
wire-faithful per-call rendering, no replay explorers, no redesign of
runtime ABI / export contract / WS2 resume semantics.
