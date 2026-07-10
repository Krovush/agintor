# Workstream 3: State, Memory, and Durability

## Outcome

- Preserve the Workstream 1 and Workstream 2 boundaries. Workstream 3 adds durability, indexing, reconstruction, and query surfaces around the existing runtime/export boundary. It does not redefine solve-time behavior.
- Keep the exported runtime as the owner of live execution and resume semantics. Workstream 3 makes those semantics restartable, inspectable, and auditable from persisted artifacts.
- Keep `CheckpointEnvelope` as the canonical restart artifact, but upgrade the persisted state around it with typed working-memory, recovery, environment, memory-lineage, and trace-cursor records.
- Turn short-term runtime provenance into durable first-class lineage instead of leaving it recoverable only from checkpoint blobs and runtime-event JSON.
- Turn long-term memory into a durable versioned graph with write lineage, tombstones, contradiction markers, retrieval diagnostics, and exact-first retrieval preserved after reload.
- Replace branch isolation based on deep-copying live shell objects with reconstructable subsystem snapshot boundaries.
- Make hosted-call persistence session-scoped, trace-context-indexed, and rebuildable into build, solve, and runtime-task views without reissuing provider calls.

## Prerequisites

- Workstream 1 export/build/runtime packaging is already implemented.
- Workstream 2 runtime execution, checkpoint publication, side-effect receipts, runtime events, and runtime-owned resume are already implemented.
- The current repository state, including uncommitted diffs, is the source of truth. Plan this workstream against the code that exists now, not the original WS2 draft.

## Sequence Position

- Start this workstream only after Workstream 2 freezes execution-plan semantics, branch execution semantics, checkpoint publication, side-effect receipt reconciliation, runtime event shapes, and runtime-owned resume transport.
- Workstream 4 depends on Workstream 3 for durable run artifacts, queryable checkpoint lineage, replayable traces, and auditable recovery outcomes.
- Workstream 5 depends on Workstream 3 for durable trace-store topology, trace-context indexing, and recovery and memory-lineage anchors.

## Boundaries

- Own persistence contracts for runtime-owned state, checkpoint lineage, short-term provenance, long-term-memory durability, environment fingerprints, typed recovery records, and trace-store topology.
- Own reconstruction of persisted state back into live runtime objects inside the bundled runtime kernel.
- Own durable trace grouping, grouped-trace rebuild APIs, and the indexed state layer for persisted runtime lineage.
- Keep solve-time scheduling policy, checkpoint timing, branch concurrency rules, tool sandbox execution semantics, provider transport semantics, export packaging, and factory-search policy outside this workstream.
- Keep provider-side raw envelope richness, wire-faithful hosted-call rendering, and transport-specific capture upgrades outside this workstream. Those belong to Workstream 5 and must build on the durable layout frozen here.
- Keep validation and test traces outside mutation surfaces. This workstream persists runtime artifacts and query indexes; it does not widen write authority.

## Non-Goals

- Replacing Workstream 2 resume semantics
- Replacing `RunStore` with a parallel checkpoint authority
- Introducing `checkpoint_store.py`, `replay_store.py`, or remote database infrastructure
- Redesigning runtime ABI meaning or reopening Workstream 1 export contracts
- Pulling Workstream 5 provider-capture richness forward into this workstream

## Frozen Inherited Context from WS1 and WS2

### 1. Export and runtime boundary are already fixed

- `RUNTIME_ABI_VERSION = "agintor-runtime-abi-v5"`.
- `KERNEL_VERSION = "agintor-kernel-v1"`.
- `STORAGE_SCHEMA_VERSION = "agintor-storage-v3"`.
- Exported runtimes already bundle `runtime_sdk/`, deployment contracts, runtime profiles, kernel manifests, and runtime fingerprints.
- `RuntimeHost.inspect()` and `RuntimeHost.solve()` already validate the runtime boundary.
- Capability exchange is already real and includes backend support, tool runtimes, checkpoint support, side-effect receipts, resume support, runtime isolation policy, supported guarantees, and environment requirements.

Treat those version axes and capability boundaries as fixed inputs. Workstream 3 may persist more metadata around them, but it must not change their meaning.

### 2. Run roots, attempts, and request bundles already exist

- `RunStore.create_run()` already creates durable run roots under `workspace/runs/<run_id>/`.
- The current base run-root layout is:

```text
workspace/
  runs/
    <run_id>/
      run_manifest.json
      request/
      attempts/
      checkpoints/
      traces/
      events/
      artifacts/
      side_effects/
```

- `RunManifest` already persists `run_id`, `run_root`, `request_id`, `evaluation_unit_id`, `request_mode`, `runtime_hash`, `runtime_abi`, `storage_schema_version`, `runtime_backend`, `task_id`, `seed`, `trace_context`, `current_attempt_id`, `latest_checkpoint_ref`, and lifecycle fields.
- `AttemptManifest` already persists `attempt_id`, `run_id`, `run_root`, `sequence_no`, `launch_kind`, `resumed_from_checkpoint_ref`, `workspace_root`, `latest_checkpoint_ref`, and lifecycle and failure timestamps.
- Request persistence already happens before launch and already stores the execution-unit request envelope plus request, plan, task, and runtime-identity material under `request/`.

Extend this lifecycle. Do not introduce a second run-state root.

### 3. Request grouping semantics are already real

- `ExecutionUnitRequestEnvelope.request_kind` already supports:
  - `runtime_solve_request`
  - `runtime_task_invocation`
  - `runtime_task_invocation_group`
- `RuntimeTaskInvocation` already carries `evaluation_unit_id`, `episode_kind`, and `episode_step_index`.
- `RunManifest.request_id` does not necessarily mean one task invocation. In grouped execution it may identify an evaluation-unit request rather than a single benchmark task.

Workstream 3 indexes must therefore support run-level, request-level, evaluation-unit-level, and episode-level queries without assuming a one-request/one-task model.

### 4. Checkpoint and resume baseline already exists

- `CheckpointEnvelope` is already the canonical restart artifact.
- Current checkpoint schema version is `agintor.checkpoint-envelope.v3`. Workstream 3 bumps it to `agintor.checkpoint-envelope.v4` when `working_state_summary` and `trace_cursor` become typed records. Update the envelope identity gate in `_restore_from_checkpoint` in the same change so no v3 envelope ever loads into a v4 runtime.
- The envelope already includes:
  - runtime identity (`runtime_abi`, `storage_schema_version`, `runtime_hash`, `runtime_backend`)
  - run and attempt identity (`run_id`, `run_root`, `attempt_id`)
  - request lineage (`request_id`, `origin_request_id`, `source_checkpoint_ref`)
  - execution identity (`plan_id`, `task_id`, `seed`, `sequence_no`, `boundary`, `created_at`)
  - resume gating (`resume_eligible`, `resume_ineligibility_reason`)
  - `plan_snapshot`, `task_payload`, `runtime_state_snapshot`, `shell_state_snapshot`, `side_effect_ledger`, `attempt_snapshot`, `working_state_summary`, and `trace_cursor`
- Resume transport already supports:
  - run-ref resume
  - explicit checkpoint-ref resume
  - external checkpoint-store refs
  - request-id rebinding for resumed requests
  - strict and best-effort receipt reconciliation policies
- Runtime-owned resume already lives in the bundled runtime entrypoint. The host/runtime split is already correct.
- `agintor.runner.TaskRuntime` remains the stable public import path, but `agintor/runner.py` is now only a compatibility facade. Task-runtime implementation ownership lives under `agintor/task_runtime/`, and exported runtime kernels must bundle that package through `agintor/runtime_sdk/bundle.py`.

Treat this as the restart-contract baseline. Workstream 3 may replace ad-hoc persisted dictionaries with typed fields where it owns the semantics, but it must not introduce a second restart artifact.

### 5. `OpenAITraceContext` is already propagated through runtime contracts

`OpenAITraceContext` already exists and already flows through runtime request, plan, frame, branch, event, and side-effect receipt surfaces. Trace grouping must be built from that emitted context, not inferred later from filenames.

### 6. `FixedShell` already owns shell-side snapshot and restore

`FixedShell` already owns checkpoint shell snapshots and restore hooks for:

- short-term graph
- long-term graph
- message board
- open handles
- task-local tool registry
- current task, episode, and scope identity

That ownership stays fixed. Workstream 3 upgrades the persistence model around it and removes deep-copy branch isolation, but it does not move shell-state ownership out of `FixedShell`.

### 7. Runtime-side branch and receipt primitives already exist

The current codebase already has:

- `BranchPlan`
- `BranchPublication`
- `BranchState`
- `BranchResumeSnapshot`
- `SideEffectReceipt`
- `ReceiptReconciliationRecord`
- checkpoint-published runtime events
- runtime-side reconciliation and receipt-backed node restoration

Preserve those branch and receipt semantics. Build durable lineage around them instead of redefining them.

### 8. Memory baseline is narrower than the previous WS3 draft assumed

- `ShortTermGraph` already persists nodes, edges, and hidden-node reachability inside shell snapshots.
- `LongTermGraph` is currently only a flat node store. It does not yet persist explicit durable edges, write lineage, contradiction records, or retrieval diagnostics.
- `LongTermNodeType` already includes `EnvironmentFingerprint` and `ArtifactSignature`, but those are not yet backed by a real extracted environment-fingerprint persistence path.
- Retrieval already enforces exact-first ordering. Exact file-path and exact symbol matches already dominate weaker heuristics.
- The baseline memory policy already uses action vocabulary aligned to:
  - implicit new and upsert
  - `merge`
  - `refine`
  - `tombstone`

Extend memory durability from that baseline. Do not invent a conflicting second write vocabulary or weaken exact-first retrieval.

### 9. Trace persistence already exists, but only as a flat call store

`openai_trace.py` already persists canonical per-call JSON and derived markdown views, but the current topology is still a single `openai_api_traces/auto/calls` store. It is not yet grouped by session, build, solve, or runtime-task identity, and it does not yet treat trace context as a first-class index key.

### 10. Container path rewriting has an important boundary

Current container/runtime path rewriting already rewrites durable run-root paths, checkpoint refs, workspace roots, and checkpoint-store dirs between host and container mounts. The intended boundary is that opaque paths embedded inside these fields remain untouched unless they are explicitly modeled as durable path refs:

- `side_effect_ledger.receipts[*].result_ref`
- `working_state_summary`
- `trace_cursor`

Preserve and enforce that distinction. Workstream 3 must audit the current implementation against this boundary because broad recursive string replacement still appears in checkpoint-envelope path rewriting.

## Stale Assumptions to Remove from the Previous WS3 Draft

Do not spend Workstream 3 effort re-solving issues the current code already fixed.

- Host finalization already prefers a runtime-reported checkpoint ref and falls back to durable run-store lookup only when needed.
- Grouped result reduction already preserves paused outcomes when a resumable checkpoint exists.
- Sync tool execution already records launch receipts and checkpoint boundaries before the actual tool run.
- The root runnable-frontier restore fallback issue is already fixed in the current task runtime.

The debug ledger contains useful history, but Workstream 3 planning must follow the present codebase.

## Real Gaps That Workstream 3 Actually Owns

- `FixedShell.fork_branch()` still uses `copy.deepcopy()` against live shell-owned subsystems.
- There is still no indexed state layer such as `agintor/state_store.py`.
- Short-term provenance is still durable only inside checkpoint payloads, runtime-event files, and trace JSON; it is not queryable as first-class lineage.
- Long-term memory still lacks durable edges, write lineage, contradiction markers, tombstone lineage, and retrieval-diagnostic records.
- `CheckpointEnvelope.working_state_summary` and `CheckpointEnvelope.trace_cursor` are still ad-hoc dictionaries rather than typed persistence contracts.
- There is still no real extracted `EnvironmentFingerprint` record.
- There is still no typed `RecoveryAttempt` ledger.
- `openai_trace.py` still writes to a single flat call store and does not yet materialize grouped session, build, solve, or runtime-task views from canonical raw-call records.
- `TaskRuntime._restore_from_checkpoint()` in `agintor/task_runtime/checkpointing.py` currently overwrites `context.state.latest_checkpoint_ref` with the shell's latest checkpoint lookup instead of preserving the exact checkpoint selected for restore.
- `FixedShell.save_trace()` still uses a filename pattern that can collide when the same `task_id` and `seed` are emitted multiple times in one attempt.
- `DockerRuntimeExecutor._rewrite_checkpoint_envelope_paths()` still applies request-file reverse mapping to broad checkpoint payloads, including `side_effect_ledger`. Workstream 3 must narrow this to typed, explicitly modeled path fields so opaque receipt result payloads, working-memory snapshots, and trace cursors are not mutated by recursive string replacement.

## Storage and Topology Decisions

- Keep the MVP storage strategy local, inspectable, deterministic, and workspace-owned.
- Keep `RunStore` as the canonical owner of run roots, request bundles, attempts, checkpoint files, latest-checkpoint selection, and resume-target resolution.
- Add exactly one public indexed durability module: `agintor/state_store.py`. Private helpers are allowed only when they keep that module smaller without creating another authority.
- Extend the existing run-root layout rather than creating a parallel top-level state tree:

```text
workspace/
  runs/
    <run_id>/
      run_manifest.json
      request/
      attempts/
      checkpoints/
      traces/
      events/
      artifacts/
      side_effects/
      state/
        runtime_state.sqlite
        short_term/
        long_term/
        recovery/
        working_memory/
```

- Keep run-owned state under the run root. Do not add a separate `workspace/state/` hierarchy.
- Keep canonical hosted-call storage session-scoped beside the workspace rather than nesting it under one run, because one session or build may span many runs:

```text
openai_api_traces/
  sessions/<session_id>/
    calls/
      *.json
    materialization_state.json
    INDEX.md
    TRANSCRIPT.md
    builds/<build_id>/
      INDEX.md
      TRANSCRIPT.md
    solves/<request_id>/
      INDEX.md
      TRANSCRIPT.md
    runtime_tasks/<task_id>/seed_<seed>/runtimes/<runtime_hash>/requests/<request_or_evaluation_unit_id>/
      INDEX.md
      TRANSCRIPT.md
```

- Canonical raw-call JSON remains the source of truth. Grouped markdown, indexes, and transcripts are derived rebuildable surfaces.
- Canonical grouped-trace materialization state lives in `openai_api_traces/sessions/<session_id>/materialization_state.json`. That file is the authoritative session-scoped cursor and manifest for build, solve, and runtime-task finalization. Rendered `INDEX.md` and `TRANSCRIPT.md` files remain derived outputs.
- `runtime_abi`, `kernel_version`, and `storage_schema_version` remain the top-level version axes for persisted artifacts. Do not add independent nested versioning unless a record is materialized outside its parent artifact.

### Canonical artifact vs indexed-row ownership split

Freeze this split before any persistence code lands.

- Canonical JSON artifacts on disk are authoritative. Use deterministic per-record files only where that is the simplest durable shape; for high-volume provenance, deterministic per-attempt or per-checkpoint JSON/JSONL shards are preferred to avoid thousands of tiny files. Indexed rows must carry enough locator information to recover the exact record inside a shard.
  - `run_manifest.json`, `attempts/<attempt_id>/attempt_manifest.json`, `request/*`, `checkpoints/*.json`, `events/*.json`, `artifacts/*`, `side_effects/*`, `traces/*`
  - `state/short_term/*.jsonl` or `state/short_term/<checkpoint_id>.json`
  - `state/long_term/writes/*.jsonl` or `state/long_term/writes/<write_id>.json`
  - `state/long_term/edges/*.jsonl` or `state/long_term/edges/<edge_id>.json`
  - `state/long_term/retrieval/*.jsonl` or `state/long_term/retrieval/<diagnostic_id>.json`
  - `state/recovery/<recovery_attempt_id>.json`, `state/recovery/fingerprints/<fingerprint_id>.json`
  - `state/working_memory/<checkpoint_id>.json` only when `WorkingMemorySnapshot` is materialized outside its parent `CheckpointEnvelope`
  - `openai_api_traces/sessions/<session_id>/calls/*.json`
  - `openai_api_traces/sessions/<session_id>/materialization_state.json`
- Indexed rows in `state/runtime_state.sqlite` are non-authoritative:
  - Never store the only copy of any record there.
  - Every row carries a scoped `canonical_ref` pointing back to the JSON artifact that owns the payload, plus a `canonical_record_id` or JSON pointer when the artifact is a shard. Run-local refs are relative to the run root. Session-trace refs are relative to the trace session root and must be explicitly marked as session-scoped.
  - Indexes exist only to satisfy the Phase 2 query surfaces. Deleting the SQLite file must be recoverable by full rebuild from canonical JSON.
- Write ownership is fixed:
  - `RunStore` remains the only module that creates, mutates, or deletes run-root files. It calls `state_store.py` write hooks only after each durable JSON write succeeds, so the index can lag committed JSON but never lead it.
  - `openai_trace.py` remains the only module that creates, mutates, or deletes canonical session-scoped trace artifacts under `openai_api_traces/sessions/<session_id>/`, including `calls/*.json` and `materialization_state.json`. It does not become a second run-root authority.
  - `state_store.py` owns SQLite schema migrations, transaction boundaries, and rebuild-from-canonical recovery. It must not write canonical JSON artifacts on its own.
  - `FixedShell` and `memory_graph.py` call `RunStore` or its state-hook facade for run-local durability. They do not open the SQLite file directly.
  - `openai_trace.py` owns session-scoped trace writes. `RunStore` indexes trace references only when run-owned artifacts, receipts, or events link to those session-scoped calls.

### Session-scoped trace ownership relative to run-local SQLite

- Session-scoped trace artifacts are canonical outside any run root. No run-local SQLite file owns them.
- `state/runtime_state.sqlite` indexes only run-local reference rows that point from run-owned artifacts to canonical session-scoped trace artifacts via `canonical_ref`.
- When one canonical trace call or grouped session, build, solve, or runtime-task materialization entry is relevant to multiple runs, each participating run-local SQLite file may store its own reference row to the same `canonical_ref`. There is no exclusive single-run owner.
- Grouped trace materialization state is authoritative only in `openai_api_traces/sessions/<session_id>/materialization_state.json`. Any run-local row that mentions grouped materialization status is cached reference metadata and must be rebuildable from that canonical session-scoped manifest.

## Core Rules

- Do not redefine solve-time meaning. Persist around the existing runtime contracts.
- Do not split restart authority between multiple stores. `CheckpointEnvelope` plus `RunStore` remain the canonical restart path.
- Replace ad-hoc persisted dictionaries with typed contracts where Workstream 3 owns the semantics.
- Replace deep-copy branch isolation with subsystem-owned snapshot and restore boundaries.
- Preserve exact-first memory retrieval after reload.
- Index trace context and request grouping as first-class keys instead of reconstructing them heuristically from filenames.
- Preserve container path-rewrite semantics: rewrite durable run-root references, leave opaque nested payloads alone.
- Keep high-volume durability append-only and batch-friendly. Do not put synchronous SQLite writes or per-node file writes in the hot inner loop when checkpoint-boundary shards or RunStore-buffered hooks preserve the same auditability.

## Phase Ordering and Dependencies

Phases are sequential unless explicitly noted.

- Phase 0 must land before Phase 1 because Phase 1 depends on correct checkpoint identity and non-colliding trace identity.
- Phase 1 must land before broad indexing because `state_store.py` indexes rows whose shape is set by the typed contracts.
- Phase 3 may land immediately after Phase 1. Do not force the branch snapshot refactor to wait for the full query layer if the snapshot contracts are already typed.
- Phase 2 may start after Phase 1 with schema and migration scaffolding, but it must not bake in live deep-copy branch semantics. If Phase 3 is still in progress, index only stable run, attempt, request, checkpoint, event, receipt, and trace-reference rows.
- Phases 4, 5, and 6 depend on the stable state-store schema and on the Phase 3 snapshot boundary. They may be implemented in parallel once those two foundations are in place, provided they share one `state_store.py` schema version bump.

## Phase 0: Normalize the Inherited Baseline Before Adding New Durability Layers

This phase is small but mandatory. Fix the remaining baseline defects that would otherwise contaminate Workstream 3 lineage.

- Fix restore-time checkpoint identity in `agintor/task_runtime/checkpointing.py` so `context.state.latest_checkpoint_ref` keeps the exact checkpoint ref selected for restore instead of being overwritten by `self.shell.latest_checkpoint_ref(...)`. Preserve `agintor.runner.TaskRuntime` as the public facade while changing the implementation module.
- Fix `FixedShell.save_trace()` filename collisions. Include an attempt-scoped monotonic sequence or an already-available invocation key such as `task_id`, `seed`, `episode_step_index`, and a per-attempt counter so repeated invocations within one attempt never overwrite each other.
- Add or extend tests that pin the current container path-rewrite boundary so future typed `WorkingMemorySnapshot` and `TraceCursorSnapshot` changes do not accidentally broaden opaque-path rewriting, including cases where request-file reverse mapping is active.
- Add or extend tests proving grouped execution identifiers (`request_id`, `evaluation_unit_id`, `episode_kind`, `episode_step_index`) survive persisted request bundles, run manifests, resume, and indexed lookup.
- Pin and narrow container checkpoint path rewriting. Durable run-root refs and checkpoint-store refs may be rewritten; opaque nested payloads such as `side_effect_ledger.receipts[*].result_ref`, `working_state_summary`, and `trace_cursor` must remain byte-for-byte semantic payloads unless a field is explicitly promoted into a typed durable path ref.

### Phase 0 test anchors

New or extended tests must land beside existing coverage and must be named explicitly so the exit gate is verifiable:

- `tests/test_runtime_execution.py`: checkpoint-ref preservation across explicit-ref resume, run-ref resume, and external-checkpoint-store resume; trace-filename non-collision under repeated task invocations within one attempt
- `tests/test_runtime_host.py`: grouped execution identity round-trip through `RunManifest`, `ExecutionUnitRequestEnvelope`, and resume rebind
- `tests/test_container_runtime.py`: path-rewrite boundary pinned around the current set of durable run-root fields, with explicit negative assertions that opaque nested payloads (`side_effect_ledger.receipts[*].result_ref`, `working_state_summary` / `working_state`, `trace_cursor`) remain untouched even when request-file reverse mapping is active

Exit condition: the post-WS2 baseline is internally consistent enough that the new durability and indexing layers can trust checkpoint identity, trace identity, and grouping identity.

## Phase 1: Replace Ad-Hoc Persistence Payloads with Typed Contracts

Tighten the persistence contract surface before adding broad indexes. Public records that cross module, runtime, or workstream boundaries live in `agintor/schemas.py` beside the existing snapshot contracts. Private row-shape helpers may stay in `state_store.py`. The public field sets below are frozen so Workstreams 4 and 5 can plan against them. Do not rename, drop, or silently widen those fields without amending this workstream.

### Common conventions

- All new public persistence contracts are Pydantic models in the same style as the existing schemas. Private SQLite row adapters may live inside `state_store.py`.
- Timestamps follow the existing repository convention: numeric UTC epoch seconds from `now_ts()`, usually named `*_at`. Human-readable ISO timestamps are derived render output, not canonical state.
- Cross-record references use string IDs or scoped canonical refs, never nested payloads. Canonical refs use run-root-relative paths for run-local artifacts and trace-session-relative paths for session-scoped trace artifacts.
- None of these records duplicate the top-level version axes. They rely on the parent `CheckpointEnvelope`, `RunManifest`, or `AttemptManifest` for `runtime_abi`, `kernel_version`, and `storage_schema_version`.
- New public persisted contracts must reject unknown fields. Existing legacy models may be tightened only where the change is part of the v4 checkpoint/storage break and the failure mode is explicit.

### Checkpoint contract upgrade

- Bump `CheckpointEnvelope.checkpoint_schema_version` from `agintor.checkpoint-envelope.v3` to `agintor.checkpoint-envelope.v4`. Update the identity gate in `agintor/task_runtime/checkpointing.py` so a v3 envelope never loads into a v4 runtime.
- Replace `working_state_summary: Dict[str, Any]` with `working_state: WorkingMemorySnapshot`.
- Replace `trace_cursor: Dict[str, Any]` with `trace_cursor: TraceCursorSnapshot`.
- Preserve the current runtime-event cursor semantics when typing `trace_cursor`. Do not repurpose this field into the authoritative session trace materialization manifest; session materialization authority lives in `openai_api_traces/sessions/<session_id>/materialization_state.json`.
- Keep `plan_snapshot` and `task_payload` generic because they are runtime payloads rather than Workstream 3-owned typed records.
- Keep `RuntimeStateSnapshot`, `ShellStateSnapshot`, `AttemptSnapshot`, and `BranchResumeSnapshot` authoritative. Extend them only when Workstream 3 persistence requires it, and record any new fields in this workstream when added.

### `WorkingMemorySnapshot`

Small, deterministic, and derived only from accepted runtime state and verified evidence. It must not become a hidden transcript mirror.

- `current_objective: Optional[str]`
- `accepted_constraints: List[str]`
- `active_plan_summary: Optional[str]` (concise, derived from `plan_snapshot`)
- `verified_facts: List[VerifiedFactRef]` where `VerifiedFactRef` has:
  - `fact_id: str`
  - `content: str` (short claim text, not transcript text)
  - `supporting_receipt_ids: List[str]`
  - `supporting_verifier_ids: List[str]`
- `unresolved_critical_items: List[str]`
- `active_branch_refs: List[str]` (branch IDs currently runnable or paused)
- `selected_checkpoint_refs: List[str]` (checkpoint refs the runtime considers load-bearing for the current objective)
- `active_recovery_warnings: List[str]` (human-readable warnings produced by the most recent `RecoveryAttempt`)
- `captured_at: float`

### `TraceCursorSnapshot`

Sufficient to resume runtime trace writing and to link the run to session-scoped hosted-call materialization without reissuing provider calls. The checkpoint-local cursor is not the materialization authority.

- `runtime_trace_length: int`
- `latest_runtime_event: Optional[str]`
- `latest_runtime_event_sequence_no: int`
- `last_session_id: Optional[str]`
- `last_build_id: Optional[str]`
- `last_solve_request_id: Optional[str]`
- `last_runtime_task_key: Optional[str]` (canonical key described in Phase 6)
- `linked_call_ids: List[str]` (call IDs observed or referenced by this run since the prior checkpoint)
- `materialization_state_ref: Optional[str]` (session-scoped canonical ref, if known)
- `captured_at: float`

### `EnvironmentFingerprint`

Captures execution properties that materially affect recovery compatibility. Persist it once per fingerprint change, keyed by `fingerprint_id`. Make it queryable both as a lineage record and, optionally, as a memory node of type `EnvironmentFingerprint`.

- `fingerprint_id: str` (stable hash over the content fields below)
- `runtime_backend: str`
- `runtime_hash: str`
- `runtime_abi: str`
- `storage_schema_version: str`
- `kernel_version: str`
- `runtime_isolation_policy: str`
- `supported_guarantees: List[str]` (from capability exchange)
- `provider_identity: List[str]` (registered provider IDs)
- `model_class: Optional[str]`
- `sandbox_hash: Optional[str]`
- `tool_runtime_ids: List[str]`
- `dependency_digest: Optional[str]`
- `filesystem_policy: Optional[str]`
- `network_policy: Optional[str]`
- `captured_at: float`
- `source_attempt_id: Optional[str]`
- `source_checkpoint_ref: Optional[str]`

### `RecoveryAttempt`

Typed record of a resume reconciliation outcome. Workstream 3 records these results; it does not redefine the reconciliation rules emitted by Workstream 2.

- `recovery_attempt_id: str`
- `run_id: str`
- `attempt_id: str`
- `selected_checkpoint_ref: str`
- `source_checkpoint_ref: Optional[str]`
- `origin_request_id: Optional[str]`
- `rebound_request_id: Optional[str]`
- `reconciliation_policy: Literal["strict", "best_effort"]`
- `compatibility_result: Literal["exact_compatible", "degraded_compatible", "fail_closed"]`
- `source_fingerprint_id: Optional[str]`
- `current_fingerprint_id: str`
- `fingerprint_deltas: List[FingerprintDelta]` where `FingerprintDelta` has:
  - `field: str`
  - `previous: Any`
  - `current: Any`
- `receipts_reused: List[str]`
- `receipts_reissued: List[str]`
- `receipts_blocked: List[str]`
- `receipts_invalidated: List[str]`
- `blocked_node_ids: List[str]`
- `degraded_plan_node_ids: List[str]`
- `resume_explanation: str`
- `attempted_at: float`
- `completed_at: Optional[float]`

### Durable long-term memory write record (`LongTermWriteRecord`)

Align the durable write record with the runtime's existing memory-policy vocabulary. Workstream 3 must not invent a second action language.

- `write_id: str`
- `target_node_id: str`
- `action: Literal["upsert", "merge", "refine", "tombstone", "conflict"]`
- `payload_ref: str` (canonical JSON path to the node body this write introduced; it may be the parent long-term node file)
- `source_task_id: Optional[str]`
- `source_attempt_id: str`
- `source_checkpoint_ref: Optional[str]`
- `verifier_support_refs: List[str]`
- `prior_write_id: Optional[str]` (refinement chain)
- `contradiction_target_write_id: Optional[str]` (set only when `action == "conflict"`)
- `written_at: float`

### `LongTermEdgeType`

This is new Workstream 3-owned vocabulary. Keep it separate from the short-term execution-graph `EdgeType` enum in `agintor/schemas.py`.

- `DERIVED_FROM`
- `REFINES`
- `CONTRADICTS`
- `SUPPORTED_BY`

These are the only v1 long-term edge values frozen by this workstream. If later implementation needs additional long-term relation kinds, amend this workstream instead of reusing or silently widening the short-term `EdgeType` enum.

### Durable long-term memory edge record (`LongTermEdgeRecord`)

- `edge_id: str`
- `source_node_id: str`
- `target_node_id: str`
- `edge_type: str` (values from the new `LongTermEdgeType` enum in `agintor/schemas.py`; do not reuse the short-term `EdgeType` vocabulary)
- `introducing_write_id: str`
- `tombstoned: bool`
- `tombstone_write_id: Optional[str]`
- `written_at: float`

### Retrieval-diagnostic record (`RetrievalDiagnosticRecord`)

Explain why a long-term retrieval returned what it did. Exact-first dominance must survive reload.

- `diagnostic_id: str`
- `query_hash: str` (stable hash of the query text)
- `task_id: Optional[str]`
- `seed: Optional[int]`
- `request_id: Optional[str]`
- `scope_id: Optional[str]`
- `returned_node_ids: List[str]` (ordered)
- `signals: List[RetrievalSignalRow]` where `RetrievalSignalRow` has:
  - `node_id: str`
  - `rank: int`
  - `exact_file_path_hit: bool`
  - `exact_symbol_hit: bool`
  - `node_id_match: bool`
  - `verifier_support_score: float`
  - `lexical_overlap_score: float`
  - `embedding_similarity_score: float`
  - `same_task_affinity_score: float`
  - `synthesized_neighbor_expansion: bool`
- `exact_first_preserved: bool`
- `retrieved_at: float`

### Validation

- Add strict model validation and canonical serialization rules for every new typed contract above.
- Add round-trip tests that restore live runtime objects from the upgraded checkpoint and snapshot set and then rerun shell invariants.
- Add negative tests proving v3 envelopes cannot load into a v4 runtime and that unknown or extra fields on any typed record are rejected rather than silently preserved.

## Phase 2: Add the Indexed State Layer Without Splitting Ownership

### Module decision

- Add `agintor/state_store.py` as the only new public indexed persistence module in this workstream.
- Integrate it into `RunStore` lifecycle operations instead of creating a second run or checkpoint owner.

### What the indexed layer must index

At minimum, index:

- run IDs
- request IDs
- evaluation-unit IDs
- task IDs
- episode IDs and episode step indexes
- attempt IDs
- checkpoint IDs and lineage
- branch IDs
- side-effect receipt IDs
- artifact refs
- runtime-event refs
- short-term node and edge refs
- long-term node, edge, and write refs
- retrieval-diagnostic refs
- recovery attempts
- environment-fingerprint refs
- trace call IDs linked from run-owned artifacts
- run-local references to session, build, solve, request/evaluation-unit, and runtime-task grouped trace materialization entries

### Storage model

- JSON artifacts remain canonical for run manifests, attempts, checkpoints, receipts, runtime events, raw hosted-call records, and every record listed under the canonical-artifact ownership split above.
- SQLite at `state/runtime_state.sqlite` stores only indexes, lineage joins, and query-surface rows. Every row carries a scoped `canonical_ref` plus record locator back to the authoritative JSON artifact. `state_store.py` must be able to rebuild every index row from canonical JSON if the SQLite file is lost.
- Auxiliary JSON shards under `state/short_term`, `state/long_term`, `state/recovery`, and `state/working_memory` are canonical for their own records and remain subordinate to `RunStore` lifecycle.
- Session-scoped canonical trace artifacts remain outside the run root. Run-local SQLite stores only reference rows to them; it does not become the authority for session, build, solve, or runtime-task grouped trace state.

### Write path, concurrency, and transaction semantics

- `state_store.py` opens SQLite in WAL journaling mode with `synchronous=NORMAL`, `foreign_keys=ON`, and a bounded `busy_timeout` such as 5000 ms. Use short-lived or thread-local connections; never share one connection concurrently across threads.
- Every state-store write is wrapped in a single transaction and committed only after the corresponding canonical JSON write already succeeded. JSON first, index second. If the index commit fails, mark the run-local index dirty or leave a detectable version/coverage gap; the next query or lifecycle call must rebuild affected rows from canonical JSON before returning authoritative query results.
- Concurrent branch execution must never write to the index through more than one path. All state-store writes flow through a `RunStore`-owned facade that serializes writes per run root.
- `state_store.py` must declare a `STATE_STORE_SCHEMA_VERSION` constant and implement forward-only migrations. Opening an older store is allowed and must migrate in place. Opening a newer store with older code must fail closed.
- `state_store.py` exposes a `rebuild_from_canonical(run_root)` entry point that drops and reconstructs the index from JSON artifacts. This is the canonical recovery path for index corruption or version mismatch.
- `rebuild_from_canonical(run_root)` must reproduce logically equivalent indexed contents and query results. It does not need to reproduce byte-identical SQLite bytes.

### Required query surfaces

Add library-first query APIs for at least:

- latest usable checkpoint for a run, evaluation unit, or task
- checkpoints produced by a specific branch or boundary
- artifacts produced by a specific checkpoint or side-effect receipt
- branch publication lineage
- recovery outcomes by checkpoint and reconciliation mode
- long-term writes and refinements affecting a node
- retrieval-diagnostic explanations for a specific memory fetch
- grouped trace materialization status for a session, build, solve, request, evaluation unit, or runtime-task run key

## Phase 3: Replace Live Deep-Copy Branch Isolation with Reconstructable Subsystem Snapshots

The current deep-copy approach in `FixedShell.fork_branch()` is the largest remaining mismatch between Workstream 2 execution semantics and Workstream 3 durability goals.

### Required direction

- Remove live-object `copy.deepcopy()` branch isolation from `FixedShell.fork_branch()`.
- Introduce subsystem-owned snapshot and restore boundaries for branch-visible mutable state, including at least:
  - short-term graph
  - long-term graph
  - message board
  - open handles
  - task-local tool registry
  - predictor state used by runtime decisions
- Use those persisted or serializable subsystem snapshots to create branch shells.
- Keep branch publication and merge semantics exactly as frozen in Workstream 2.

### Subsystem snapshot interface (frozen contract)

Each branch-visible subsystem must implement one consistent protocol. Do not vary its signature or semantics by subsystem.

- `snapshot(self) -> SubsystemSnapshotRecord`
  - Return a frozen, serializable record whose payload can be converted back into canonical JSON.
  - The record must be safe to share across branch boundaries without further copying.
  - It must not hold references to mutable live objects. A Pydantic model is preferred; a frozen dataclass is also acceptable.
- `restore(self, snapshot: SubsystemSnapshotRecord) -> None`
  - Rebuild live subsystem state from the snapshot.
  - `restore` may copy from the snapshot's immutable payload when constructing live objects.
  - The restored subsystem must be indistinguishable from the one that produced the snapshot.
- `fork_from_snapshot(cls, snapshot: SubsystemSnapshotRecord) -> Self`
  - Classmethod factory returning a new subsystem instance initialized from an immutable snapshot.
  - This is the only path `FixedShell.fork_branch()` uses to build branch-local subsystems.

`FixedShell.fork_branch()` must call `snapshot()` once on each branch-visible subsystem of the parent, then pass those records to `fork_from_snapshot()` on the child. It must not call `copy.deepcopy()` on any shell-owned subsystem.

Slot subsystem snapshot records into the existing `ShellStateSnapshot` and `BranchResumeSnapshot` hierarchy as typed fields so checkpoint and restore share the same serialization path as branch resume.

### Important nuance

This phase removes deep copies of live runtime-owned objects as the branch-isolation mechanism. It is still acceptable for `restore` and `fork_from_snapshot` to copy from immutable snapshot records when reconstructing live objects.

### Required tests

- Branch-local mutations must not leak into the parent shell without explicit publication.
- Checkpoint and restore must remain able to reconstruct branch resume state from persisted branch snapshots.
- Concurrent branch execution must preserve current receipt, publication, and cancellation semantics.
- Every subsystem's `snapshot`, `restore`, and `fork_from_snapshot` round-trip must be tested against shell invariants.

## Phase 4: Persist Short-Term Provenance and Long-Term Memory Lineage as First-Class State

### Short-term provenance

Persist short-term provenance as queryable durable lineage, not only as checkpoint blobs.

Persist provenance at checkpoint, branch-publication, attempt-finalization, or explicit retention boundaries. Do not synchronously flush every graph mutation to disk and SQLite during the inner solve loop unless a boundary requires it.

Store at least:

- short-term nodes
- short-term edges
- hidden-node flags
- summary backlinks
- artifact lineage
- branch publication lineage
- open-handle and async-job lineage
- verifier-evidence refs
- side-effect receipt refs
- runtime-event refs

Expose query surfaces that answer questions such as:

- which checkpoint produced artifact `X`
- which summary hid raw node `Y`
- which branch published artifact `Z`
- which receipt or runtime event supports an observed state transition
- which verifier evidence supported a final output

### Long-term memory

Turn long-term memory into a durable versioned graph with:

- node records
- edge records
- write lineage
- contradiction markers
- tombstones
- verifier-support refs
- source-task lineage
- source-checkpoint lineage
- retrieval-diagnostic rows

### Write vocabulary

Durable write records must align with the runtime's real memory-policy vocabulary. Use write actions that match current runtime semantics, including:

- new node creation through `upsert`
- `merge`
- `refine`
- `tombstone`
- explicit conflict recording when a contradiction is detected

Do not invent a second durability-only action language such as `merge_support` or `refine_version` that diverges from what the runtime actually emits.

### Retrieval diagnostics

Persist why a node ranked highly, including signals such as:

- exact file-path hit
- exact symbol hit
- verifier support
- node-ID match
- lexical overlap
- embedding similarity
- same-task affinity
- synthesized neighbor expansion

Exact-first retrieval must remain dominant after reload. Durable graph richness must not weaken the target-spec retrieval rule.

## Phase 5: Add Deterministic Working Memory, Real Environment Fingerprints, and Typed Recovery Records

### Working memory

`WorkingMemorySnapshot` must remain deliberately small and always explainable. It should contain only current accepted state such as:

- current objective
- accepted constraints
- active plan summary
- verified facts
- unresolved critical items
- active branch refs
- selected checkpoint refs
- active recovery warnings if any

It must not become a hidden transcript mirror.

### Environment fingerprints

Extract real `EnvironmentFingerprint` records from runtime facts, including the execution properties that materially affect recovery compatibility, such as:

- runtime backend
- runtime hash
- runtime ABI
- storage schema version
- kernel version
- runtime isolation policy and guarantees
- provider identity
- selected model class where relevant
- sandbox hash
- tool runtime identifiers
- dependency or environment digest where available
- filesystem and network policy surfaces that affect side-effect replay

Store the fingerprint as a real recovery input and queryable lineage record, not only as a memory node.

### Recovery ledger

Add typed `RecoveryAttempt` records that classify outcomes as:

- `exact_compatible`
- `degraded_compatible`
- `fail_closed`

Each recovery attempt must record at least:

- selected checkpoint ref
- source checkpoint ref
- origin request ID and rebound request ID where applicable
- reconciliation policy
- compatibility result
- fingerprint deltas that mattered
- receipts reused, reissued, blocked, or invalidated
- blocked node IDs and degraded plan nodes
- final resume explanation

Workstream 3 records and explains recovery outcomes. It does not redefine receipt reconciliation rules or runtime-owned resume semantics.

## Phase 6: Freeze the Trace Store Topology and Rebuild Surface

### Canonical raw-call storage

Extend `openai_trace.py` from a flat `auto/calls` layout into a session-scoped store of canonical raw-call JSON records.

Each canonical record must persist first-class top-level fields for at least:

- request payload
- request metadata
- trace context (the full `OpenAITraceContext`, including resolved `session_id`)
- provider role copied from trace context or request metadata for indexing
- raw response envelope
- usage
- latency
- error
- canonical call ID
- ordering information that prevents filename collisions

### Trace-context resolution and fallback rules (required)

`OpenAITraceContext` keeps `session_id`, `build_id`, and related fields optional. The grouped topology must still behave deterministically. These rules are mandatory:

- `session_id` is required at persistence time. If the incoming `OpenAITraceContext` does not provide one, `openai_trace.py` must derive it exactly once per host process from the stable triple `(host_session_start_time, host_pid, host_machine_id_hash)` and cache it. Write the resolved `session_id` back into the persisted call record's `trace_context` and, when a checkpoint is later published, into `TraceCursorSnapshot.last_session_id`.
- `build_id` is optional. When absent, still write the call under `openai_api_traces/sessions/<session_id>/calls/` and index it by session, solve, and runtime-task grouping as applicable. Skip build-scoped materialization under `builds/<build_id>/`. Do not synthesize a placeholder build ID.
- Solve grouping: when `request_id` is present, materialize under `solves/<request_id>/`. `request_id` is guaranteed by WS1 and WS2 runtime contracts for every solve-time call, so absence indicates a bug rather than a fallback path.
- Runtime-task grouping: materialize under `runtime_tasks/<task_id>/seed_<seed>/runtimes/<runtime_hash>/requests/<request_or_evaluation_unit_id>/` only when all of `task_id`, `seed`, `runtime_hash`, and either `request_id` or `evaluation_unit_id` are present. If any are missing, skip runtime-task materialization for that call without inventing substitute keys.
- The canonical runtime-task materialization key is `task_id|seed|runtime_hash|request_or_evaluation_unit_id`. The shorter `task_id|seed|runtime_hash` triple may be used only as an aggregate index, never as the unique materialization key.
- If `evaluation_unit_id` is not part of `OpenAITraceContext`, derive it only from the run-owned request envelope or run manifest during grouped rebuild. Do not infer it from path names.
- There is no unscoped bucket. If a caller cannot supply enough input for required `session_id` derivation, fix the caller instead of writing to an `_unscoped/` directory.
- Add one resolution helper in `openai_trace.py` so every call site resolves trace context through the same path. Grouped rebuild logic must use that same helper.

### Grouped materialization

Materialize rebuildable grouped views for:

- session
- build, only when `build_id` is present
- solve and request
- runtime task grouped by `task_id`, `seed`, `runtime_hash`, and request or evaluation-unit identity, only when all required fields are present

Grouped markdown and indexes must be derived from canonical raw-call records plus resolved trace context. They are not a second source of truth.

Grouped transcript generation must call one shared renderer in `openai_trace.py`. Workstream 3 owns the grouping and rebuild path; Workstream 5 owns provider-side capture richness and per-call render fidelity. The boundary must still prevent grouped transcripts from inventing request or response body content, and it must keep local orchestration metadata in call context instead of splicing it into visible model traffic.

`materialization_state.json` must be a typed session-scoped manifest with, at minimum:

- `session_id`
- `schema_version`
- `last_finalized_call_id`
- `known_call_ids`
- `materialized_build_ids`
- `materialized_solve_request_ids`
- `materialized_runtime_task_keys`
- `pending_build_ids`
- `pending_solve_request_ids`
- `pending_runtime_task_keys`
- `errors`
- `updated_at: float`

### Rebuild surface

- Add library-first rebuild APIs that regenerate grouped views after interruption.
- Persist materialization cursors so grouped trace finalization can resume deterministically.
- Allow run-state indexes to point from checkpoints, runtime events, and receipts to trace call IDs without duplicating canonical call payloads.

### Ownership split with Workstream 5

- Workstream 3 freezes trace-store topology, grouped rebuild logic, and trace-context indexing.
- Workstream 5 later upgrades provider-side capture richness and per-call rendering fidelity inside that topology.

## Regression Gates

- Run-store continuity tests proving:
  - latest usable checkpoint selection still works
  - external checkpoint refs still resolve correctly
  - grouped request identities still round-trip through persistence
  - restore preserves the exact selected checkpoint ref
- Snapshot round-trip tests for:
  - `CheckpointEnvelope` (v4)
  - `RuntimeStateSnapshot`
  - `ShellStateSnapshot`
  - `WorkingMemorySnapshot`
  - `EnvironmentFingerprint`
  - `RecoveryAttempt`
  - `TraceCursorSnapshot`
  - `LongTermWriteRecord`, `LongTermEdgeRecord`, `RetrievalDiagnosticRecord`
- Negative envelope-identity tests proving a v3 envelope cannot load into a v4 runtime and that unknown fields on any typed record are rejected.
- Branch-isolation tests proving branch state no longer depends on live deep copies and does not leak without publication.
- Subsystem snapshot protocol tests for each branch-visible subsystem covering `snapshot`, `restore`, and `fork_from_snapshot` round-trips.
- Persistence tests for:
  - short-term provenance durability
  - long-term write lineage
  - contradiction and tombstone handling
  - exact-first retrieval stability after reload
  - retrieval-diagnostic stability
- Recovery tests proving exact-compatible, degraded-compatible, and fail-closed outcomes are driven by persisted fingerprints and receipt lineage.
- `state_store.py` integrity tests proving:
  - every index row resolves back to its canonical JSON artifact
  - `rebuild_from_canonical` reproduces the same normalized row contents, key coverage, and query results after deleting `state/runtime_state.sqlite`
  - opening a newer-version store with older code fails closed
- Trace-store tests proving:
  - raw-call records rebuild identical grouped session, build, solve, and runtime-task views without reissuing provider calls
  - grouped transcripts are rendered through the shared `openai_trace.py` renderer and do not place local orchestration metadata inside visible request or response bodies
  - `materialization_state.json` round-trips as a typed session-scoped manifest and resumes finalization from its cursor after interruption
  - `TraceCursorSnapshot` preserves runtime-event cursor semantics and only links to session materialization state; it does not duplicate or override the session-scoped manifest
  - duplicate invocation patterns no longer overwrite prior traces
  - `session_id` is resolved deterministically when absent from the incoming context, and the resolved value appears in the persisted record
  - calls with missing `build_id` skip `builds/` materialization without creating placeholder directories
  - calls with incomplete runtime-task identity, including missing request or evaluation-unit identity, skip `runtime_tasks/` materialization without inventing substitute keys
- Container path-rewrite tests proving:
  - run-root and checkpoint-store paths still rewrite correctly
  - opaque nested payloads inside side-effect results, working-memory snapshots, and trace cursors remain untouched unless they are explicitly modeled as durable run-root refs

## Handoff to Workstream 4

Workstream 4 receives:

- durable run artifacts
- queryable checkpoint lineage
- typed recovery records
- deterministic working-memory snapshots
- persisted short-term provenance
- durable long-term memory lineage and diagnostics
- rebuildable grouped trace views

Treat those artifacts as fixed inputs for resumable evaluation, held-out reporting, and resumable search.

## Handoff to Workstream 5

Workstream 5 receives:

- a stable session, build, solve, and runtime-task trace topology
- first-class persisted trace context and trace cursors
- typed recovery and environment-fingerprint surfaces
- durable memory and receipt-lineage anchors

Workstream 5 must not redesign storage topology. It upgrades provider capture richness and render fidelity inside the durable layout frozen here.

## Acceptance Gates

1. Workstream 3 preserves Workstream 1 and Workstream 2 solve-time and export semantics and adds only durability, indexing, reconstruction, and query surfaces.
2. `RunStore` remains the canonical run and checkpoint owner, and no parallel checkpoint authority is introduced.
3. The run-root layout remains authoritative and now includes a `state/` subtree under each run root without introducing a second top-level state tree.
4. Full runtime state round-trips through persisted snapshots and passes shell invariants after restore.
5. `CheckpointEnvelope` remains the canonical restart artifact.
6. Restore and resume preserve the exact checkpoint selected by the operator or runtime rather than replacing it with a later latest-checkpoint lookup.
7. Branch isolation no longer depends on deep-copying live shell objects.
8. Retained runs leave behind queryable short-term provenance and durable long-term memory lineage with exact-first retrieval preserved after reload.
9. Working memory, environment fingerprints, recovery attempts, and trace cursors are typed persisted contracts rather than ad-hoc dictionaries.
10. Canonical raw-call records plus the session-scoped materialization manifest rebuild grouped session, build, solve, and runtime-task views without reissuing provider calls, and repeated task invocations do not overwrite each other.
11. Grouped execution identity (`request_id`, `evaluation_unit_id`, `episode_kind`, `episode_step_index`) remains queryable across run manifests, checkpoints, state indexes, and trace grouping; runtime-task trace materialization does not collapse distinct request or evaluation-unit runs into one task/seed/runtime bucket.
12. Container path rewriting preserves durable run-root semantics while leaving opaque nested payloads untouched.
13. `OpenAITraceContext` in `agintor/schemas.py` is the trace-correlation source of truth. Requirements in `TRACE_AND_PLANNING_IMPROVEMENTS_PLAN.md` that target trace storage and grouping topology are superseded by this workstream. Requirements there about goal-conditioned benchmark planning remain outside Workstream 3 scope.
14. `CheckpointEnvelope.checkpoint_schema_version` is bumped to `agintor.checkpoint-envelope.v4` in the same change that lands the typed `WorkingMemorySnapshot` and `TraceCursorSnapshot` fields, and the restore identity gate rejects v3 envelopes in a v4 runtime.
15. `state_store.py` holds only indexes. Dropping `state/runtime_state.sqlite` on any run root and calling `rebuild_from_canonical` reconstructs every index row from canonical JSON without loss.
16. High-volume provenance can be sharded by checkpoint or attempt, and every indexed row still resolves to an exact canonical record through a scoped ref plus record locator.

## File Ownership

- `agintor/schemas.py`: typed persistence contracts, checkpoint contract upgrades, environment fingerprints, recovery records, trace cursors, long-term lineage records, and retrieval-diagnostic records
- `agintor/run_store.py`: run-root lifecycle, checkpoint authority, and state-store integration hooks
- `agintor/state_store.py`: SQLite lineage and index query surfaces for run-owned durable state
- `agintor/shell.py`: shell snapshot and restore lifecycle plus branch snapshot boundaries
- `agintor/memory_graph.py`: durable short-term and long-term graph semantics, write lineage, tombstones, contradictions, and retrieval diagnostics
- `agintor/runner.py`: compatibility facade that preserves `agintor.runner.TaskRuntime` and legacy test patch points only; do not add runtime implementation here
- `agintor/task_runtime/base.py`: composed `TaskRuntime` class, constructor, public `run_task`, `resume_from_checkpoint`, provider-environment isolation, and provider-usage helpers
- `agintor/task_runtime/checkpointing.py`: checkpoint publication upgrades, checkpoint restore, frame snapshots, runtime-state restore, run-result construction, recovery recording, and restore-time checkpoint-identity correctness
- `agintor/task_runtime/side_effects.py`: side-effect receipt persistence and reconciliation, including filesystem-write reconciliation
- `agintor/task_runtime/branching.py` and `agintor/task_runtime/branch_execution.py`: branch publication, branch resume snapshots, horizontal branch launch/resume/run/cancel paths, and branch provider preparation
- `agintor/task_runtime/execution_loop.py`, `frames.py`, `plan_helpers.py`, `operations.py`, `memory.py`, `tooling.py`, `bounded_io.py`, and `verification.py`: solve-time runtime-host execution helpers split by existing responsibility; preserve behavior and private method names used by tests
- `agintor/runtime_api.py`: resume rebind helpers for upgraded typed checkpoint fields and grouped request-identity continuity
- `agintor/openai_trace.py`: canonical session-scoped raw-call storage, canonical session-scoped materialization manifests, grouped trace finalization, rebuild APIs, and collision-free call identity
- `agintor/container_runtime.py`: path-rewrite support for upgraded typed persistence fields
- `agintor/runtime_sdk/`: runtime-owned restore entrypoints, state-reconstruction wiring, and bundled kernel source list for all `agintor/task_runtime/` modules
- persistence and runtime tests adjacent to `tests/test_runtime_execution.py`, `tests/test_runtime_host.py`, and `tests/test_container_runtime.py`

## Deferred

- Remote or managed state stores
- Cross-runtime shared memory services
- Provider-side raw envelope richness upgrades before Workstream 5
- Wire-faithful per-call rendering completeness before Workstream 5
- GUI replay explorers
