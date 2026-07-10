# Implementation Report of Product-First Runtime Prompt and Durability Fix Plan

This document records what was implemented against the plan. Each section gives the prior state, the current state, and the files that were touched. No code is reproduced here.

## 1. Identity model and schemas

### Prior state

`OpenAITraceContext` carried benchmark-shaped fields only (request, task, seed, runtime hash, episode kind/step) and accepted `episode_kind` values such as `single_task`, `user_request`, `benchmark_duplicate`, and `batch`. Those literals were sprinkled across request builders and trace persistence, conflating internal benchmark machinery with user-facing prompt categories. There was no schema-level concept of a factory chat or a runtime chat session; `RuntimeSolveRequest` had no carryover slot, and `SolveResult` had no post-message state slot.

### Current state

`OpenAITraceContext` is the single correlation envelope and now carries factory-chat identity (`factory_chat_id`, `factory_message_id`, `factory_message_index`) and runtime-session identity (`runtime_session_id`, `runtime_message_id`, `runtime_message_index`) alongside the existing benchmark fields. `episode_kind` is narrowed to `Optional[Literal["transfer_episode"]]` with a validator that coerces every legacy literal to `None`. `RuntimeTaskInvocation` applies the same narrowing.

New schemas formalize the chat identities:

- `FactoryChatIdentity`, `FactoryMessage` — factory project chats and per-message planning provenance.
- `RuntimeSessionIdentity`, `RuntimeSessionMessage` — runtime chat sessions and per-message persistence pointers.
- `RuntimeSessionSeed` — the carryover envelope (long-term graph snapshot, predictor snapshot, short-term carryover rows) that the host injects into the next message.

`GoalSpec` gained `amendment_index` and `amendment_history` so factory follow-ups have a stable lineage. `RuntimeSolveRequest` gained an optional `session_seed`; benchmark mode rejects it. `SolveResult` gained `post_message_long_term_graph`, `post_message_predictor_snapshot`, and `post_message_short_term_export`.

### Files touched

- `agintor/schemas.py`

## 2. Trace topology

### Prior state

Trace materialization grouped every record under `runtime_tasks/<task>/seed_<n>/runtimes/<runtime>/.../requests/<request>/` regardless of whether the call came from a factory build, a runtime chat message, or a benchmark task. User-prompt records were tagged with the invented `episode_kind="user_request"`, which polluted the trace tree with episode-shaped folders that had nothing to do with transfer episodes. There was no path for browsing a factory project or a runtime chat session as a coherent unit.

### Current state

Trace records are dispatched into one of three product-shaped groups by a single dispatcher:

- `factory_projects/<chat>/m<idx>_<msg>/` — factory chat messages.
- `runtime_sessions/<runtime>/<session>/m<idx>_<msg>/` — runtime chat messages.
- `benchmark_tasks/<task>/seed_<n>/<runtime>/<request>/` — benchmark tasks. Transfer episodes append a `__step_<n>` suffix to the leaf instead of nesting an `episodes/transfer_episode/` subtree, which keeps paths within Windows length limits.

Records that supply none of the three identity sets are kept in the flat session view but are not grouped further. `materialization_state.json` was renamed-in-place: it now exposes `factory_message_keys`, `runtime_session_message_keys`, and `benchmark_task_keys` (with matching `materialized_*` and `pending_*` lists) instead of the old `runtime_task_keys`. Persisted call records carry `trace_group_kind` and `trace_group_key` instead of `runtime_task_key`.

The benchmark grouping helper is now strictly `transfer_episode`-only; it returns no episode parts for any other input. Request builders no longer set `episode_kind` defaults, so user-request and single-shot benchmark records persist with `episode_kind=None`.

### Files touched

- `agintor/openai_trace.py`
- `agintor/runtime_api.py`
- `agintor/state_store.py`

## 3. Docker checkpoint path rewriting

### Prior state

Docker finalization rewrote durable-run metadata paths (run root, attempt manifests, checkpoint refs) from `/mnt/...` to host paths but did not rewrite the `open_handles` table inside the checkpoint envelope's shell-state snapshot, nor inside any branch resume snapshot's nested shell-state snapshot. A run finalized on the host therefore retained container-coordinate `working_directory`, `stdout_path`, `stderr_path`, and `artifact_refs` values that no longer pointed at anything reachable from the host.

### Current state

Checkpoint envelope rewriting now normalizes `open_handles[*]` paths through the same host-projection logic used for run-level metadata, both at the top-level shell-state snapshot and inside every branch resume snapshot. Receipt result payloads, runtime artifacts, and other replay-state values continue to stay in container coordinates so resume replay remains faithful.

### Files touched

- `agintor/container_runtime.py`

## 4. Runtime kernel session hydration and export

### Prior state

The kernel had no notion of a chat session. Each `run_task` started from a fresh shell with no carryover; long-term memory and predictor state from the previous user prompt did not survive into the next one. There was no kernel-side mechanism to export end-of-message persistent state, and `runtime_entry` did not look at any session-shaped fields on the request.

### Current state

`TaskRuntime.run_task` accepts an optional `RuntimeSessionSeed` and threads it into the execution loop. When the seed is present and the request is not a checkpoint resume, the `MemoryMixin` hydrates the long-term graph and predictor snapshot from the seed and replays the seed's short-term carryover rows as recap entries on the message board. At the end of a `user_request` run, the kernel exports the post-message long-term graph, predictor snapshot, and a condensed short-term summary; `runtime_entry` populates the corresponding `post_message_*` fields on `SolveResult` so the host can persist them. Benchmark runs leave those fields empty.

### Files touched

- `agintor/runtime_sdk/runtime_entry.py`
- `agintor/task_runtime/base.py`
- `agintor/task_runtime/execution_loop.py`
- `agintor/task_runtime/memory.py`

## 5. Runtime session persistence and CLI

### Prior state

There was no on-disk concept of a runtime chat session. Every `agintor solve --prompt …` invocation started a new, isolated run; users had no way to continue a conversation with a built runtime, and the trace tree had no per-session view. The `solve` command exposed no session flags.

### Current state

A new `RuntimeSessionStore` persists sessions under `<runtime_dir>/.runtime_sessions/<session_id>/`, with a manifest plus per-message directories holding the prompt, request, response, result, long-term graph, predictor snapshot, and a short-term-carryover boundary file. Sessions are pinned to the runtime hash that was current at creation; loading a session against a rebuilt runtime fails with a clear mismatch error.

The `solve` command accepts `--session <id>` to continue an existing session and `--new-session` to force a fresh one (default in chat mode). The host loads the runtime to get its hash, allocates or loads a session, asks the store for a `RuntimeSessionSeed` derived from the latest completed message, builds a session-aware request, runs the solve, and records the resulting message (including its lifecycle state and any checkpoint ref). Benchmark mode rejects session flags. JSON output now includes a top-level `target` field and, for chat mode, a `runtime_session` block with `session_id`, `message_id`, `message_index`, `parent_message_id`, and `runtime_hash`.

### Files touched

- `agintor/runtime_session_store.py` (new)
- `agintor/runtime_api.py`
- `agintor/runtime_host.py`
- `agintor/cli.py`

## 6. Factory chat persistence and goal amendment

### Prior state

There was no concept of a factory chat. `agintor build-runtime` was a one-shot command that took a freeform prompt, a `--destination`, and unconditionally rebuilt a runtime from scratch. There was no way to amend a goal, no message ledger, no record of which planning artifacts produced which leader runtime, and no way to evolve a project across multiple invocations. Goals had no amendment lineage.

### Current state

A new `FactoryChatStore` persists chats under `<project_dir>/.factory_chat/`, with a manifest and per-message directories that snapshot every planning artifact (`goal_spec.json`, `success_criteria.json`, `benchmark_plan.json`, `verifier_bundle.json`, `runtime_plan.json`, `deployment_contract.json`, `export_summary.json`, `build_summary.json`) plus the prompt and the message metadata.

`goal_rubric.amend_goal_spec` produces an amended `GoalSpec` from a prior goal plus a follow-up instruction. The amended goal preserves `goal_id`, bumps `amendment_index`, appends the instruction to `amendment_history`, and re-derives keywords, phrases, target families, capabilities, constraints, and success criteria from the combined prompt. The hosted-provider planning refinement path explicitly preserves the amendment fields across the merge.

The factory pipeline is now reachable via three layers:

- `_run_factory_pipeline` is the shared internal body. It accepts either a freeform prompt or a prebuilt `GoalSpec`, and an optional seed runtime source directory. When a seed source is provided, the evolution seed is initialized as a copy of that prior leader runtime (kernel bundle and bytecode excluded) before the new runtime profile and deployment contract are written.
- `build_runtime_from_goal` and `build_runtime_from_followup` are thin wrappers around the shared pipeline.
- `apply_factory_message(project_dir, instruction, …)` is the single product-level entry point. The first invocation against a fresh project directory creates the chat and runs the initial build; subsequent invocations load the prior message's goal spec, amend it, run the follow-up build seeded from the prior leader runtime, and append a new message to the chat ledger.

### Files touched

- `agintor/factory_chat_store.py` (new)
- `agintor/goal_rubric.py`
- `agintor/runtime_builder.py`

## 7. Factory chat CLI

### Prior state

`agintor build-runtime "<goal>" --destination <dir>` accepted the goal as a positional argument, required a `--destination` option, exposed a `--force` flag for overwriting existing destinations, and had no concept of follow-up invocations. The output JSON was a flat dump of `BuiltRuntimeResult` with no chat or message identity.

### Current state

`agintor build-runtime <project_dir> --prompt "<message>"` treats the project directory as the chat target. The first call creates the factory chat in place and runs the initial build; subsequent calls amend the prior message's goal and rebuild the runtime in place. `--destination` and `--force` are gone; the project directory is both the chat scope and the export destination, and follow-ups overwrite the runtime by design. JSON output includes a top-level `target: "factory"` field, a `factory_chat` block with chat and message identity (including `runtime_hash`, `runtime_dir`, `build_id`, `parent_message_id`), and the full build result.

### Files touched

- `agintor/cli.py`

## 8. Tests

### Prior state

The test suite covered runtime execution, runtime host, container runtime, and durability contracts against the old trace topology, the old `episode_kind` literals, and the old build pipeline. There was no coverage for factory chats, runtime sessions, the new trace dispatcher, or session-aware kernel behavior.

### Current state

Three new test files cover the new product surface:

- `tests/test_factory_chat.py` — `FactoryChatStore` create/load/persistence/ordering, `amend_goal_spec` correctness (preserved `goal_id`, bumped `amendment_index`, extended `amendment_history`, rejection of empty instructions), and `apply_factory_message` initial-vs-followup routing.
- `tests/test_runtime_sessions.py` — `RuntimeSessionStore` create/load/runtime-hash pinning/recording/seeding, with `failed`-message carryover skipped in favor of the latest completed message; benchmark-mode rejection of `session_seed`; and integration tests that exercise `RuntimeHost.solve` to confirm post-message state is populated for `user_request` runs, absent for `benchmark` runs, and that a `RuntimeSessionSeed` round-trips through the kernel.
- `tests/test_trace_topology.py` — the trace grouping dispatcher selects the right group kind, each kind writes to the correct view directory, persisted records carry `episode_kind=None` for chat-mode requests, and the validator coerces every legacy episode literal to `None`.

The existing test files were updated to match the new contracts:

- `tests/test_durability_contracts.py` — assertions migrated to the new `TraceMaterializationState` field names and the flatter benchmark view directory layout.
- `tests/test_runtime_host.py` — batch-request tests assert `episode_kind=None` for non-transfer invocations and `episode_step_index` is populated for transfer-episode steps.
- `tests/test_container_runtime.py` — Docker durable-run rewriting test asserts that `open_handles[*]` paths are rewritten to host paths inside the checkpoint envelope.

The default offline suite went from 189 tests to 214 tests; all pass.

### Files touched

- `tests/test_factory_chat.py` (new)
- `tests/test_runtime_sessions.py` (new)
- `tests/test_trace_topology.py` (new)
- `tests/test_durability_contracts.py`
- `tests/test_runtime_host.py`
- `tests/test_container_runtime.py`