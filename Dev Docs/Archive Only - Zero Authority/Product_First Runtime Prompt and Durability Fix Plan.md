# Product-First Runtime UX, Identity, and Durability Plan

## Product axioms (load-bearing)

A user is always in **one of three** interaction modes. Every internal contract must serve exactly one of them:

1. **Factory chat.** The user is talking to Agintor about a build/evolution project. The chat is the project. The first message starts the project; later messages refine it. The project lives at a directory `<project_dir>` and produces an exported runtime in-place.
2. **Runtime chat.** The user is talking to a built runtime. The chat is the session. The first message starts a session; later messages continue it with carried-over runtime memory. A runtime can host many independent sessions.
3. **Benchmark task.** The user (or evaluator) is running a runtime against a benchmark task. No chat semantics — pure single-shot or transfer-episode execution.

**Episodes belong only to mode 3.** Every other use of `episode_kind` is wrong. Specifically:
- `single_task`, `user_request`, `benchmark_duplicate`, `batch` are not episodes; they must not appear in `episode_kind`.
- `transfer_episode` is the only legitimate value of `episode_kind`.
- For modes 1 and 2, identity is `factory_chat_id`/`message_id` or `runtime_session_id`/`message_id`, not episode-shaped.

## What this plan delivers

- A clean CLI surface where each command implies exactly one mode and the directory implies the chat/project identity.
- Real chat/session behavior on both factory and runtime sides, with identity, persisted message ledger, and runtime-memory carryover.
- A trace topology that mirrors the product (factory projects, runtime sessions, benchmark tasks) instead of the current `runtime_tasks/<task>/seed/.../e_<hash>/` clutter.
- The two existing bugs (Docker open-handle path leak; `episode_user_request` trace pollution) fixed as part of the larger correction, not as standalone patches.

Backward compatibility with prior checkpoints, prior trace layouts, prior CLI shape, or prior `episode_kind` literals is **not** preserved (per project rules).

## Final CLI surface

```
agintor init-runtime <runtime_dir>                                   # unchanged: scaffold a baseline runtime
agintor build-runtime <project_dir> --prompt "<message>"             # factory chat: first message creates project; subsequent invocations on same <project_dir> are follow-ups
agintor solve <runtime_dir> <task_id> --suite <suite>                # benchmark task (no session)
agintor solve <runtime_dir> --prompt "<message>" [--session <id>]    # runtime chat: first message auto-allocates session_id; subsequent messages reuse with --session
agintor eval <runtime_dir> --suite <suite> --seeds 0,1,2             # unchanged: multi-seed benchmark eval
agintor evolve <runtime_dir> --steps N                               # unchanged: bounded search against the runtime's frozen suite
```

Output JSON for every command includes a top-level `target` (`"factory"` | `"runtime"`) and the relevant identity payload (`factory_chat`, `runtime_session`, or neither for unscoped commands). No `--target` flag exists; the command implies the target.

## Identity model

### `FactoryChatIdentity`

Stable identity of a factory chat (project). Persisted at `<project_dir>/.factory_chat/manifest.json`.

```python
class FactoryChatIdentity(BaseModel):
    chat_id: str                  # stable id of the chat/project; derived once at first message
    project_dir: str              # absolute path of the project
    goal_id: str                  # frozen at first message; preserved across follow-up amendments
    runtime_provider: str         # the runtime provider used (frozen at first message)
    agintor_provider: str         # the factory-side provider (frozen at first message)
    runtime_backend: str          # frozen at first message
    created_at: float
    message_count: int
    last_message_id: Optional[str]

class FactoryMessage(BaseModel):
    message_id: str               # unique per message in this chat
    message_index: int            # 0 for initial, 1+ for follow-ups
    parent_message_id: Optional[str]
    chat_id: str
    prompt: str
    created_at: float
    build_id: str                 # build identity for this message's evolution pass
    leader_runtime_hash: str
    leader_runtime_dir: str       # within the message's evolution workspace
    goal_spec_path: str
    success_criteria_path: str
    benchmark_plan_path: str
    verifier_bundle_path: str
    runtime_plan_path: str
    deployment_contract_path: str
    export_summary_path: str
    build_summary_path: str
```

### `RuntimeSessionIdentity`

Stable identity of a runtime chat. Persisted at `<runtime_dir>/.runtime_sessions/<session_id>/manifest.json`.

```python
class RuntimeSessionIdentity(BaseModel):
    session_id: str               # stable id of the session
    runtime_dir: str
    runtime_hash: str             # runtime hash when session was created (sessions are pinned to a runtime hash)
    created_at: float
    message_count: int
    last_message_id: Optional[str]

class RuntimeSessionMessage(BaseModel):
    message_id: str
    message_index: int
    parent_message_id: Optional[str]
    session_id: str
    request_id: str               # = solve_request.request_id for this turn
    prompt: str
    created_at: float
    boundary_state_path: Optional[str]   # condensed short-term carryover; populated only on completed turns
    long_term_graph_path: Optional[str]  # LongTermGraphSnapshot exported at message-end
    predictor_snapshot_path: Optional[str]
    result_path: Optional[str]    # the SolveResult
    response_path: Optional[str]  # the RuntimeSolveResponse
    checkpoint_ref: Optional[str] # set if message paused mid-execution; resume continues this message, not the next one
    lifecycle_state: Literal["completed", "paused", "failed", "cancelled"]
```

Output paths are `Optional` because the kernel populates them only when the corresponding artifact is produced. A failed message records its metadata but leaves `boundary_state_path`, `long_term_graph_path`, and `predictor_snapshot_path` unset; the next message in the session uses the last **completed** message as its carryover source, so failed messages do not poison session memory.

A runtime session is **pinned to a runtime hash**. If the runtime under `<runtime_dir>` is rebuilt and its hash changes, a session created against the old hash refuses to continue. New runtime hash → new session is required.

Distinction from resume:
- `RuntimeHost.resume()` continues a paused message (an unfinished execution). It keeps the same `message_id`, restoring the in-flight plan from a checkpoint.
- A new chat message in an existing session is a **fresh execution** that hydrates only persistent runtime memory (long-term graph, predictor) from the prior message's boundary state. Open handles, side-effect ledger, in-flight plan, and message board are not carried over.

## Trace topology

`OpenAITraceContext` gains:

```python
factory_chat_id: Optional[str] = None
factory_message_id: Optional[str] = None
factory_message_index: Optional[int] = None
runtime_session_id: Optional[str] = None
runtime_message_id: Optional[str] = None
runtime_message_index: Optional[int] = None
```

`OpenAITraceContext.episode_kind` becomes `Optional[Literal["transfer_episode"]]`. All other historical values are removed.

Trace materialization writes the following grouped views under `openai_api_traces/sessions/<session>/`:

```
calls/                                       # flat per-call view (unchanged)
INDEX.md                                     # flat index (unchanged)
TRANSCRIPT.md                                # full chronological transcript (unchanged)

factory_projects/<chat_slug>/
  m<idx>_<msg_slug>/
    TRANSCRIPT.md
    INDEX.md

runtime_sessions/<runtime_slug>/<session_slug>/
  m<idx>_<msg_slug>/
    TRANSCRIPT.md
    INDEX.md

benchmark_tasks/<task>/seed_<n>/<runtime>/
  <request_slug>/                                  # single-shot benchmark requests
    TRANSCRIPT.md
    INDEX.md
  <request_slug>__step_<n>/                        # transfer_episode steps (one leaf per step)
    TRANSCRIPT.md
    INDEX.md

builds/<build_id>/                           # per-build factory message snapshot (unchanged)
solves/<request_id>/                         # per solve_request flat view (unchanged)
```

The `runtime_tasks/` directory (with its `e_<hash>` clutter) is removed. The new `benchmark_tasks/` view replaces it for benchmark traces and is cleaner. Factory and runtime chat traces never appear there.

The middle segments `messages/`, `requests/`, and `episodes/<episode_id>/` are intentionally absent so the deepest trace leaf stays comfortably under the Windows MAX_PATH=260 limit when nested inside pytest tmpdirs and CI runners. Transfer-episode steps are disambiguated by a `__step_<n>` suffix on the leaf rather than by an extra subdirectory level. Chat/session manifests do not live under the trace tree; they live with the chat itself under `<project_dir>/.factory_chat/` and `<runtime_dir>/.runtime_sessions/`.

`runtime_task_trace_key()` is replaced by three identity-specific helpers plus a single dispatcher that decides which one applies to a given trace context:

```python
benchmark_task_trace_key(*, request_id, task_id, seed, runtime_hash,
                        evaluation_unit_id=None, episode_kind=None, episode_step_index=None)
factory_message_trace_key(*, factory_chat_id, factory_message_id, factory_message_index=None)
runtime_message_trace_key(*, runtime_hash, runtime_session_id, runtime_message_id, runtime_message_index=None)

trace_grouping_key(trace_context) -> (group_kind, group_key) | None
```

A trace record is grouped under at most one of the three trees. Records that supply none of the three identity sets stay in the flat session view but are not promoted into a grouped subtree.

`materialization_state.json` exposes `factory_message_keys`, `runtime_session_message_keys`, and `benchmark_task_keys` (each with matching `materialized_*` and `pending_*` siblings) instead of `runtime_task_keys`.

## Request envelope changes

`RuntimeSolveRequest` gains:

```python
session_seed: Optional[RuntimeSessionSeed] = None
```

```python
class RuntimeSessionSeed(BaseModel):
    session_id: str
    message_index: int
    parent_message_id: Optional[str]
    long_term_graph: LongTermGraphSnapshot = Field(default_factory=LongTermGraphSnapshot)
    predictor_snapshot: Optional[PredictorSnapshot] = None
    short_term_carryover: List[Dict[str, Any]] = Field(default_factory=list)  # explicit, narrow: prior message's user_message + assistant_summary nodes
```

`SolveResult` gains:

```python
post_message_long_term_graph: Optional[LongTermGraphSnapshot] = None
post_message_predictor_snapshot: Optional[PredictorSnapshot] = None
post_message_short_term_export: List[Dict[str, Any]] = Field(default_factory=list)  # condensed assistant turn(s) for next message's short-term recap
```

These three fields are populated by the kernel at the end of every runtime-chat solve. They are unset for benchmark mode (avoiding cost on the dominant evaluation path).

`RuntimeSolveRequest.mode` keeps its existing literal pair `Literal["benchmark", "user_request"]`. The product-first chat distinction is carried entirely by request and trace identity (`runtime_session_id`, `runtime_message_id`, `runtime_message_index`, `session_seed`), not by a mode rename — renaming the `"user_request"` literal would be cosmetic churn across many call sites without changing behavior. The chat framing surfaces in the CLI command (`solve <runtime_dir> --prompt "<message>"`) and in the JSON output (`target: "runtime"`, `runtime_session: { ... }`). Single-shot `--prompt` invocations without `--session` allocate a fresh session implicitly and run as `mode="user_request"` with `session_seed=None`. Benchmark transfer episodes still use `mode="benchmark"` plus `episode_kind="transfer_episode"`. A model-validator on `RuntimeSolveRequest` rejects `session_seed` in benchmark mode.

## Memory carryover semantics

When a runtime-chat solve runs with a non-null `session_seed`:

- Before plan execution, the kernel hydrates `Shell.long_term_graph` from `session_seed.long_term_graph` and (if present) `Shell.predictor_state` from `session_seed.predictor_snapshot`.
- The kernel inserts the `short_term_carryover` rows into the new run's `MessageBoard` as a recap header before any new short-term context is generated.
- Open handles, side-effect ledger, in-flight plan, frame stack, and message board sequence numbers are **not** seeded. They are fresh per message.
- After plan execution terminates (verified or controlled failure), the kernel exports `post_message_long_term_graph`, `post_message_predictor_snapshot`, and a condensed `post_message_short_term_export` (typically the user prompt + the assistant terminal answer) for the next message's seed.

Crash-resume of an in-flight chat message remains a checkpoint resume against the same `message_id`; it never advances to the next message.

## Factory chat amendment

A factory follow-up (`build-runtime <project_dir> --prompt "..."` against an existing project) executes:

1. Load `<project_dir>/.factory_chat/manifest.json` and the latest message metadata.
2. Load the prior `goal_spec.json` from the latest message's snapshot. (Other planning artifacts are regenerated from the amended goal rather than read back, so only the goal spec is required for follow-up routing.)
3. Run `amend_goal_spec(prior_goal, instruction, ...)`:
   - Local: deterministic merge — combine `prior_goal.normalized_goal` with the new instruction, re-derive `goal_keywords`/`goal_phrases`/`target_families`/`required_capabilities`/`constraints`/`deployment_preferences`/`success_criteria`, **preserve `goal_id`** (the project identity), bump `amendment_index`, and append the canonicalized instruction to `amendment_history`.
   - Hosted refinement runs against the amended goal via the existing `_maybe_provider_refine_planning` flow, which explicitly preserves `amendment_index`/`amendment_history` across the merge so a hosted model cannot drift those identity fields. A dedicated hosted-amendment endpoint that also enforces `runtime_backend` and `runtime_provider` invariants is deferred (see Out of scope).
4. Regenerate `success_criteria`, `benchmark_plan`, `verifier_bundle`, `runtime_plan`, and `deployment_contract` from the amended goal via the shared `_run_factory_pipeline` body. Run consistency checks (`_plan_consistency_check`); repair via `_repair_planning_artifacts` if needed.
5. Run evolution **starting from the prior leader** as the seed runtime — `_run_factory_pipeline` copies the prior leader's runtime directory into the build workspace's seed slot (excluding `__pycache__`, `*.pyc`, `*.pyo`, and the kernel bundle directory, which is regenerated rather than vendored) before writing the new runtime profile and deployment contract on top.
6. Re-validate, select a new leader, **replace** the contents of `<project_dir>` with the new leader's runtime, append the new export bundle. The replacement is in-place via the shared pipeline's `force=True` path.
7. Persist the message under `<project_dir>/.factory_chat/messages/<idx>_<msg_id>/` with copies of all planning artifacts (`goal_spec.json`, `success_criteria.json`, `benchmark_plan.json`, `verifier_bundle.json`, `runtime_plan.json`, `deployment_contract.json`, `export_summary.json`, `build_summary.json`) plus `prompt.txt` and `metadata.json`. The chat manifest's `message_count` and `last_message_id` are refreshed.

Identity preservation: `goal_id` and `chat_id` are frozen at first message and preserved across follow-ups by `amend_goal_spec` and the chat store. Runtime/agintor provider names and `runtime_backend` are recorded on the chat manifest at creation and read back from there for consistency checks; the local amendment path cannot flip them. Hosted-provider enforcement of those invariants is deferred to the hosted amendment endpoint.

The runtime at `<project_dir>` is overwritten in place. Atomic shadow-rename rollback on partial failure (so the prior message's runtime survives a botched follow-up) is deferred; today, a failed follow-up evolution leaves the project in an indeterminate state and requires a re-run.

## Implementation phases

Phases are ordered so each one stands on its own and unlocks the next. Tests at each phase boundary.

### Phase 1 — Schema, trace context, identity types

Files: `agintor/schemas.py`, `agintor/openai_trace.py`.

- Add `FactoryChatIdentity`, `FactoryMessage`, `RuntimeSessionIdentity`, `RuntimeSessionMessage`, `RuntimeSessionSeed`.
- Extend `OpenAITraceContext` with the six new factory/session fields. Apply the same `episode_kind` narrowing on `RuntimeTaskInvocation`.
- Narrow `OpenAITraceContext.episode_kind` to `Optional[Literal["transfer_episode"]]`. Attach a `field_validator(mode="before")` that coerces every legacy literal (`single_task`, `user_request`, `benchmark_duplicate`, `batch`) and any other non-canonical value to `None`, so downstream code can rely on the narrowed shape even when reading older serialized payloads.
- Add `post_message_*` fields to `SolveResult` (and propagate through `RuntimeSolveResponse`, `RunResult` only where they need to flow back to the host — see Phase 4).
- Keep `RuntimeSolveRequest.mode` as `Literal["benchmark", "user_request"]` (the chat framing is carried by identity fields and the CLI surface, not by the literal). Add `session_seed: Optional[RuntimeSessionSeed]` with a model validator that rejects it in benchmark mode. Also expose `runtime_session_id`, `runtime_message_id`, and `runtime_message_index` directly on the request envelope so the kernel and trace layer don't need to dig into the seed to find them.
- Add new trace-key helpers (`factory_message_trace_key`, `runtime_message_trace_key`, `benchmark_task_trace_key`) plus the dispatcher `trace_grouping_key`; replace `runtime_task_trace_key`. The benchmark helper composes only `transfer_episode` parts when `episode_kind == "transfer_episode"` and `episode_step_index is not None`.
- Replace trace materialization grouping (`_write_grouped_views`) to write the new flattened directory layout (`factory_projects/`, `runtime_sessions/`, `benchmark_tasks/`); drop `runtime_tasks/`.
- `materialization_state.json` field set is replaced in place: `runtime_task_keys` is removed and the three new key lists (`factory_message_keys`, `runtime_session_message_keys`, `benchmark_task_keys`) plus their `materialized_*` and `pending_*` siblings are added. The schema literal stays at `agintor.trace-materialization.v1`; the file is rebuilt from records on each persist, so older state files are overwritten without migration.

### Phase 2 — Builder hygiene and Docker handle fix

Files: `agintor/runtime_api.py`, `agintor/container_runtime.py`, `agintor/runtime_host.py`, `agintor/runtime_sdk/runtime_entry.py`.

- Strip every `episode_kind="user_request" | "single_task" | "benchmark_duplicate" | "batch"` literal from request builders. Only `runtime_batch_request_for_tasks` may set `episode_kind="transfer_episode"`, and only when the task is `transfer_scored` and has an `episode_id`.
- Extend `runtime_solve_request_for_user_request()` (name kept) to accept the optional `runtime_session_id`, `runtime_message_id`, `runtime_message_index`, and `session_seed` parameters. When `session_seed` is set and the explicit identity overrides are not, the builder takes the session id and message index from the seed. `runtime_solve_request_for_task()` keeps its name and signature for benchmark.
- `compile_execution_plan_from_solve_request` no longer defaults `episode_kind`. `runtime_trace_context()` and `build_trace_context()` propagate the new factory and runtime-session identity fields onto the trace context emitted with each plan.
- `_runtime_task_episode_key_parts()` is removed; benchmark trace keys use a new `_benchmark_episode_key_parts()` that emits parts only for `episode_kind == "transfer_episode"` with a real `episode_step_index`. A backward-compatible `runtime_task_trace_key` thin wrapper is kept for internal call sites that already pass benchmark-shaped arguments.
- `RuntimeHost._group_run_request_order()` and other internal `episode_kind` checks remain — they were already correct (only branching on `transfer_episode`).
- In `container_runtime.py`:
  - Add `_rewrite_shell_state_snapshot_paths(snapshot_payload, run_mount_root, checkpoint_store_dir)` that validates a `ShellStateSnapshot`, rewrites every handle in `open_handles.handles[*]` via `_rewrite_async_handle_paths()`, returns the normalized payload.
  - In `_rewrite_checkpoint_envelope_paths`, after the existing `branch_resume_snapshots` rewrite, also rewrite `payload["shell_state_snapshot"]` via the new helper.
  - In `_rewrite_branch_resume_snapshot_paths`, replace the bare `payload["shell_state_snapshot"] = dict(...)` with the new helper.
- Wire kernel `runtime_entry.py` to read `request.session_seed` (Phase 3 actually applies it; Phase 2 only ensures the field round-trips through stdio/docker without loss).

### Phase 3 — Runtime kernel session hydration

Files: `agintor/task_runtime/memory.py`, `agintor/task_runtime/execution_loop.py`, `agintor/task_runtime/checkpointing.py`, `agintor/runtime_sdk/runtime_entry.py`, `agintor/runner.py` (façade).

- Add `TaskRuntime._apply_session_seed(seed: RuntimeSessionSeed)` on `MemoryMixin` that:
  - Hydrates `self.shell.long_term_graph` from `seed.long_term_graph` (reuse the existing graph-hydration path used by checkpoint restore, but isolated to long-term).
  - Hydrates predictor state from `seed.predictor_snapshot` if present.
  - Inserts `seed.short_term_carryover` rows as message-board recap with provenance `{ "kind": "session_carryover", "session_id": …, "parent_message_id": … }`.
- Thread `session_seed` through `TaskRuntime.run_task(...)` and `ExecutionLoopMixin._run_execution_plan(...)`. After `Shell.reset_for_task()` and before plan execution, call `_apply_session_seed` when the request carries a `session_seed` and there is no checkpoint envelope. Resume from a checkpoint always wins over session-seed hydration (resume already implies the same `message_id`).
- Add `MemoryMixin._export_post_message_state()` that returns `(LongTermGraphSnapshot, PredictorSnapshot, condensed_short_term_rows)` from the current shell at run termination. The condensed short-term export captures the assistant's terminal answer plus any verifier evidence and task notes from the turn — narrow enough to be useful as recap, small enough not to dominate the next message's context.
- In `runtime_sdk/runtime_entry._solve(...)`, after `runner.run_task(...)` returns, when `request.mode == "user_request"` call `runner._export_post_message_state()` and populate `SolveResult.post_message_long_term_graph`, `post_message_predictor_snapshot`, and `post_message_short_term_export`. Benchmark mode (`mode="benchmark"`) leaves them at their default empty values.

### Phase 4 — Runtime session host + CLI

Files: new `agintor/runtime_session_store.py`; `agintor/runtime_host.py`; `agintor/cli.py`; `agintor/runtime_api.py`.

- New `RuntimeSessionStore(runtime_dir: Path)`:
  - `create_session(*, runtime_hash, session_id=None) -> RuntimeSessionIdentity`
  - `load_session(session_id, *, runtime_hash) -> RuntimeSessionIdentity`
  - `has_session(session_id) -> bool`, `list_sessions() -> list[str]`
  - `next_message_index(session_id) -> int`
  - `allocate_message_id(session_id, *, message_index, prompt) -> str`
  - `latest_message(session_id) -> RuntimeSessionMessage | None`
  - `latest_completed_message(session_id) -> RuntimeSessionMessage | None`
  - `messages(session_id) -> list[RuntimeSessionMessage]`
  - `seed_for_next_message(session_id) -> RuntimeSessionSeed | None` — sourced from `latest_completed_message`, so failed messages do not poison the carryover.
  - `record_message(session_id, message, *, prompt_text, request_payload, response, result) -> RuntimeSessionMessage` — writes the prompt, request, response, and result; if the result carries `post_message_*` exports, persists them as `boundary_state.json`, `long_term_graph.json`, and `predictor_snapshot.json` and updates the message's `*_path` fields.
  - Persists under `<runtime_dir>/.runtime_sessions/<session_id>/...`.
  - Refuses to load a session whose `runtime_hash` differs from the current runtime's hash; raises `RuntimeSessionMismatchError` (a subclass of `AgintorError`) with a clear message.
- `RuntimeHost.solve(...)` and `RuntimeHost.resume(...)` keep their existing signatures. Session resolution lives in the CLI (`solve_cmd`), not in the host: the CLI loads the runtime, allocates or loads a session against the runtime hash, asks the store for the next-message seed, attaches the seed and identity to the request via `runtime_solve_request_for_user_request`, calls `host.solve(...)`, and on return records a new `RuntimeSessionMessage` with the response. Keeping the host signature unchanged avoids spreading session knowledge across host internals that already work for benchmark mode.
- On post-launch failure that produces no usable boundary state, the message is recorded with `boundary_state_path` (and the long-term/predictor paths) unset and `lifecycle_state="failed"`. The next message in the session uses the most recent **completed** message as its carryover source.
- Resume of a paused message keeps the existing `message_id` semantics — `RuntimeHost.resume(...)` is unchanged. In-place rewrite of a `RuntimeSessionMessage` when a paused message resumes to completion is deferred (see Out of scope).
- CLI (`solve_cmd`):
  - Add `--session <id>` (continue) and `--new-session` (force-allocate; default when `--prompt` is used and `--session` is omitted).
  - Reject `--session` together with a positional `task_id` (benchmark + session is meaningless). Reject `--session` with `--new-session`.
  - JSON output: top-level `target: "runtime"`, plus `runtime_session: { session_id, message_id, message_index, parent_message_id, runtime_hash }` for chat mode, or the existing benchmark identity fields for benchmark mode.

### Phase 5 — Factory chat host + CLI

Files: new `agintor/factory_chat_store.py`; `agintor/runtime_builder.py`; `agintor/cli.py`; `agintor/goal_rubric.py`.

- New `FactoryChatStore(project_dir: Path)`:
  - `create_chat(*, goal_id, runtime_provider, agintor_provider, runtime_backend, chat_id=None) -> FactoryChatIdentity` — refuses if a chat already exists at this path.
  - `load_chat() -> FactoryChatIdentity` — raises `FactoryChatError` if uninitialized.
  - `has_chat() -> bool`
  - `next_message_index() -> int`
  - `allocate_message_id(*, message_index, prompt) -> str`
  - `latest_message() -> FactoryMessage | None`, `messages() -> list[FactoryMessage]`
  - `record_message(message, *, prompt_text, planning_artifacts: dict[field_name, source_path]) -> FactoryMessage` — copies each named planning artifact into the per-message directory and updates the message metadata's `*_path` fields to point at the copies, then refreshes the manifest.
  - Persists under `<project_dir>/.factory_chat/<MESSAGES_DIR>/<idx>_<msg_id>/`. The chat directory is the chat's own home and may coexist with arbitrary other content in `<project_dir>`; the store does not gate on whether the project dir is "empty enough", so no `--force` flag is needed for the chat store itself.
- New `goal_rubric.amend_goal_spec(prior_goal: GoalSpec, instruction: str, *, runtime_provider_name=None, default_runtime_backend=None) -> GoalSpec`:
  - Local: deterministic. Combines `prior_goal.normalized_goal` with the new instruction (joined by `Follow-up:`) and re-derives `goal_keywords`, `goal_phrases`, `target_families`, `required_capabilities`, `constraints`, `deployment_preferences`, and `success_criteria` from the combined prompt. Preserves `goal_id`. Bumps `amendment_index` and appends the canonicalized instruction to `amendment_history` (both new fields on `GoalSpec`).
  - Hosted refinement reuses the existing `_maybe_provider_refine_planning` flow against the amended goal, with explicit preservation of `amendment_index`/`amendment_history` across the merge so a hosted model cannot drift those fields. A dedicated `mode="planning_amendment"` endpoint is deferred (see Out of scope).
- `runtime_builder` exposes three layered entry points:
  - `_run_factory_pipeline(*, goal_input: GoalSpec | str, destination, workspace, provider, steps, mutator_type, profile_path, runtime_backend, artifact_mode, force, seed_runtime_source: Path | None = None) -> BuiltRuntimeResult` — the shared pipeline body. Accepts either a freeform prompt or a prebuilt `GoalSpec`. When `seed_runtime_source` is set, the seed runtime is initialized as a copy of that prior leader runtime (excluding `__pycache__`, `*.pyc`, `*.pyo`, and the kernel bundle directory, which is regenerated rather than vendored) before the new runtime profile and deployment contract are written.
  - `build_runtime_from_goal(goal_prompt, *, destination, ...)` — initial-build wrapper. Calls `_run_factory_pipeline` with `seed_runtime_source=None`.
  - `build_runtime_from_followup(prior_goal, instruction, *, destination, ..., seed_runtime_source) -> BuiltRuntimeResult` — follow-up wrapper. Calls `amend_goal_spec` then `_run_factory_pipeline` with the amended goal and the prior leader runtime as seed source. `force=True` is passed to the shared pipeline because follow-ups overwrite the project's runtime in place by design.
  - `apply_factory_message(project_dir, instruction, *, workspace, provider, steps, mutator_type, profile_path, runtime_backend, artifact_mode) -> FactoryMessageOutcome` — the single product-level entry point that the CLI calls. Routes to `build_runtime_from_goal` when the project has no chat yet (and creates the chat after the build succeeds) or to `build_runtime_from_followup` against the prior message's recorded goal when it does. Always records the resulting `FactoryMessage` (including copies of every planning artifact) before returning, then re-reads the chat manifest so the returned identity reflects the post-recording `message_count` and `last_message_id`.
- `FactoryMessageOutcome` is a small dataclass carrying `chat: FactoryChatIdentity`, `message: FactoryMessage`, and `result: BuiltRuntimeResult`.
- The per-message snapshot under `.factory_chat/messages/<idx>_<msg_id>/` holds copies of `goal_spec.json`, `success_criteria.json`, `benchmark_plan.json`, `verifier_bundle.json`, `runtime_plan.json`, `deployment_contract.json`, `export_summary.json`, and `build_summary.json`, plus `prompt.txt` and `metadata.json` for the message itself. The prior project's `.factory_chat/` is preserved across follow-ups; only the top-level runtime files at `<project_dir>` are replaced.
- CLI (`build_runtime_cmd`):
  - Replace positional `prompt` argument with `<project_dir>` positional; `--prompt` (or `--prompt-file`) carries the message.
  - Remove `--destination` option (redundant — the project dir is the destination).
  - Remove `--force` option — follow-ups overwrite the project's runtime in place by contract; there is no "preserve a prior runtime alongside" semantics. (Atomic shadow-rename is a follow-up; see Out of scope.)
  - The command invokes `apply_factory_message(<project_dir>, <prompt>, ...)`. First call creates the chat and runs the initial pipeline; subsequent calls run the follow-up pipeline.
  - JSON output: top-level `target: "factory"`, plus `factory_chat: { chat_id, project_dir, message_id, message_index, parent_message_id, runtime_hash, runtime_dir, build_id }` and a `build` block carrying the full `BuiltRuntimeResult`.

### Phase 6 — Tests

Files: existing `tests/test_runtime_execution.py`, `tests/test_durability_contracts.py`, `tests/test_container_runtime.py`, `tests/test_runtime_host.py`; new `tests/test_runtime_sessions.py`, `tests/test_factory_chat.py`, `tests/test_trace_topology.py`.

Mandatory new coverage:

1. `tests/test_trace_topology.py`:
   - The grouping dispatcher `trace_grouping_key` selects the correct group kind for factory, runtime-session, and benchmark contexts; returns `None` for contexts with none of the three identity sets.
   - A factory message context materializes records under `factory_projects/<chat>/m0_<msg>/...`, with no `runtime_sessions/` or `benchmark_tasks/` siblings emitted.
   - A runtime-session context materializes records under `runtime_sessions/<runtime>/<session>/m0_<msg>/...`.
   - A benchmark single-shot context materializes records under `benchmark_tasks/<task>/seed_<n>/<runtime>/<request>/...`.
   - A benchmark transfer-episode context materializes step records under `benchmark_tasks/<task>/seed_<n>/<runtime>/<request>__step_<n>/...`.
   - No persisted call carries `episode_kind` in `{single_task, user_request, benchmark_duplicate, batch}`. The `episode_kind` validator coerces every legacy literal to `None` on construction.

2. `tests/test_container_runtime.py::test_rewrite_durable_run_paths_rewrites_metadata_and_preserves_runtime_payloads`:
   - Existing assertions stay.
   - Top-level `CheckpointEnvelope.shell_state_snapshot.open_handles[*].working_directory|stdout_path|stderr_path|artifact_refs` rewrite from `/mnt/runs/...` to the host run root and from `/mnt/checkpoints/...` to the host checkpoint store.
   - Same coverage for `runtime_state_snapshot.branch_resume_snapshots[*].shell_state_snapshot.open_handles[*]`.
   - Receipt result payloads remain in container coordinates (ledger semantics unchanged).

3. `tests/test_runtime_sessions.py`:
   - `RuntimeSessionStore` create/load round-trip; `runtime_hash` pinning rejects loads with a different hash via `RuntimeSessionMismatchError`.
   - `record_message` persists prompt, request payload, response, result, and (when present on the result) `boundary_state.json`, `long_term_graph.json`, `predictor_snapshot.json`; the manifest's `message_count` and `last_message_id` advance after recording.
   - `seed_for_next_message` returns a seed sourced from the latest **completed** message; a failed intermediate message does not poison the carryover.
   - `RuntimeSolveRequest(mode="benchmark", session_seed=…)` is rejected at validation time.
   - Integration: `RuntimeHost.solve` for `mode="user_request"` populates the `post_message_*` exports; `mode="benchmark"` leaves them empty.
   - Integration: a follow-up `host.solve` that supplies a `RuntimeSessionSeed` round-trips the carryover through the kernel and produces a fresh `post_message_long_term_graph` for the next turn.

4. `tests/test_factory_chat.py`:
   - `FactoryChatStore` create/load round-trip; double-create raises `FactoryChatError`; `load_chat` on an uninitialized project raises `FactoryChatError`.
   - `record_message` copies provided planning artifacts into the per-message directory and updates the message's `*_path` fields to the copies; `messages()` returns messages ordered by index; `next_message_index` advances after each recorded message.
   - `amend_goal_spec` preserves `goal_id`, bumps `amendment_index`, appends the canonicalized instruction to `amendment_history`, and re-derives keywords/phrases/families from the combined prompt; an empty instruction is rejected.
   - `apply_factory_message` routing: against a fresh project_dir it calls `build_runtime_from_goal` and creates the chat; against an existing chat it loads the prior message's `goal_spec`, calls `build_runtime_from_followup` with the prior leader as `seed_runtime_source`, and appends the next `FactoryMessage` (with `parent_message_id` = prior message and `chat_id` unchanged). The build pipeline is mocked in this test because running real evolution under a pytest tmpdir routinely exceeds Windows MAX_PATH; the routing logic itself is what's under test.

5. `tests/test_durability_contracts.py`:
   - All trace-key assertions migrated to `benchmark_task_trace_key` shape.
   - `TraceMaterializationState` exposes `factory_message_keys`, `runtime_session_message_keys`, `benchmark_task_keys` (plus `materialized_*` and `pending_*` siblings); old `runtime_task_keys` field is gone.
   - Persisted call records carry `trace_group_kind` and `trace_group_key`, not `runtime_task_key`.
   - Benchmark transfer-episode keys retain `episode_transfer_episode|step_N` parts.
   - Concurrent persistence test rebuilds grouped views against the flattened layout.

6. `tests/test_runtime_host.py`:
   - Batch-request invocations have `episode_kind=None` for non-transfer tasks (no `single_task` or `benchmark_duplicate` literal leakage).
   - Transfer-scored invocations have `episode_kind="transfer_episode"` and `episode_step_index` populated from `episode_order`.
   - `runtime_solve_request_for_user_request` (with its extended signature accepting session identity and `session_seed`) does not set `episode_kind` on the trace context.

Deferred test coverage (tracked under Out of scope):

- Failure-mode of factory follow-up that would change `goal_id`, runtime provider, or runtime backend (requires the hosted `mode="planning_amendment"` endpoint to enforce these as identity invariants).
- Atomic shadow-rename rollback when a follow-up evolution fails partway (today's contract is overwrite-in-place; rollback is a follow-up).
- In-place rewrite of a `RuntimeSessionMessage` when a paused message resumes to completion.
- Plan-digest stability under chat/session identity changes (digest is already independent of trace metadata; an explicit assertion is a hardening follow-up).

Acceptance for this phase: the default offline pytest suite passes with the three new test files added (target: ≥210 passing tests, up from 189 before this plan landed) and no test references the legacy `episode_kind` literals or the legacy `runtime_tasks/` trace tree.

## Out of scope

- Migration of any pre-existing checkpoints, traces, or `.agintor_runs/` workspaces.
- A web/HTTP surface around chat/session.
- Multi-user isolation of `<runtime_dir>/.runtime_sessions/`.
- Long-term memory eviction policies on session carryover (every message pulls the full prior long-term graph; no compaction across messages yet — this is a follow-up after baseline correctness lands).
- Concurrent writes to one session from multiple processes (the session store may take a coarse file lock; concurrent same-session writes will be added when actually needed).
- Hosted-provider planning amendment as a dedicated `mode="planning_amendment"` endpoint. The local `amend_goal_spec` path is in scope; hosted refinement runs against the amended goal via the existing `_maybe_provider_refine_planning` flow with explicit `amendment_index`/`amendment_history` preservation, but a dedicated hosted amendment endpoint that enforces identity invariants (e.g. refusing instructions that would flip `runtime_backend` or `runtime_provider`) is deferred.
- Atomic shadow-directory replacement of the project's runtime on follow-up failure. Today the runtime is overwritten in place; if a follow-up evolution fails midway, the project is left in an indeterminate state and the user must re-issue the follow-up. A shadow-rename contract that preserves the prior runtime intact on failure is a follow-up.
- In-place rewrite of a `RuntimeSessionMessage` when a paused message resumes to completion. `RuntimeHost.resume(...)` keeps its existing signature this round; the session-message rewrite hook is a follow-up.

## Sequencing decision for the implementation pass

Phases 1–4 are required to deliver runtime chat. Phase 5 is required to deliver factory chat. Phase 6 follows each preceding phase. If implementation must be split, runtime chat (1–4) is the smaller and more impactful chunk and ships first; factory chat (5) ships next. Both phases land before this plan is closed.
