# Total Agintor Reorganization Plan - Refined

## Purpose

Reorganize the Agintor source tree so filesystem ownership matches the product:

- Factory: builds, evolves, validates, and exports runtimes.
- Runtime host: parses runtime requests and dispatches local or Docker execution.
- Runtime kernel: bundled solve-time implementation.
- Storage/state: durable runs, checkpoints, sessions, traces, and artifacts.
- Evaluation/search: benchmark pressure, verifier evidence, scoring, archive, and optimizer loop.

This is a behavior-preserving source reorganization for a fresh post-WS2 commit. It is not permission to redesign APIs, change runtime semantics, change scoring, add WS4/WS5 features, or preserve old disposable checkpoints/exported runtimes.

## Current Evidence

Measured from the current tracked repo:

- Source: 59 tracked `agintor/*.py` / `agintor/**/*.py` files, 28,367 non-blank LOC.
- Tests: 8 tracked test files, 11,332 non-blank LOC.
- Total tracked Python: 67 files, 39,699 non-blank LOC.
- Exact-module AST import scan found no internal source cycles.
- `runner.py` has already been split into `task_runtime/`; do not resurrect the old runner-centric plan.

Highest import hubs:

| Module | Importers |
|---|---:|
| `agintor/schemas.py` | 44 |
| `agintor/utils.py` | 38 |
| `agintor/exceptions.py` | 26 |
| `agintor/runtime_api.py` | 18 |
| `agintor/runtime_profile.py` | 15 |
| `agintor/providers.py` | 13 |

Largest first-party modules:

| File | Non-blank LOC | Main problem |
|---|---:|---|
| `agintor/runtime_api.py` | 2,617 | Request loading, prompt adaptation, plan compilation, trace identity, resume rebinding, protocol builders, and result shaping are mixed. |
| `agintor/schemas.py` | 2,050 | Every contract type lives in one import hub. |
| `agintor/container_runtime.py` | 1,845 | Docker launch, mounts, path rewriting, request/response rewriting, checkpoint rewrite, and recovery are mixed. |
| `agintor/runtime_builder.py` | 1,693 | Factory planning, pipeline orchestration, export, follow-up handling, and validation are mixed. |
| `agintor/state_store.py` | 1,598 | SQLite schema, indexing, canonical replay, JSON coercion, writers, and query helpers are mixed. |
| `agintor/task_runtime/branch_execution.py` | 1,167 | Branch budget, provider allocation, execution, resume, cancellation, and result construction are mixed. |
| `agintor/runtime_host.py` | 1,134 | Host facade, preflight, backend dispatch, resume resolution, validation, and finalization are mixed. |
| `agintor/openai_trace.py` | 1,078 | Provider-agnostic tracing is hidden behind an OpenAI-specific name and mixed with rendering/materialization. |
| `agintor/tool_runtime.py` | 866 | Registry, sandbox, execution, async handles, and validation are mixed. |

## Scope Rules

- Keep the package rooted at `agintor/`. Do not adopt `src/` in this pass.
- Move and split existing code only. Do not add placeholder packages, empty architecture files, toy demos, fallbacks, or temporary behavior patches.
- Do not move root docs, create `docs/`, add `README.md`, clean scratch files, or reorganize workstream docs in the source-reorg commit.
- Do not move tracked prompt templates or ignored/generated baseline runtime template files in this pass.
- Do not mirror the final package tree under `tests/` yet. Update imports and add boundary tests first.
- Keep `utils.py` mostly intact for the first pass. It is small and heavily imported.
- Keep `cli.py` in place this pass. It is not one of the main architectural knots.
- Temporary shims may exist, but shims re-export only. Final internal imports must use canonical new paths.

## Target Package Shape

Create only packages that receive current code:

```text
agintor/
  core/
  contracts/
  providers/
  learning/
  tracing/
  storage/
  runtime/
    profile.py
    loader.py
    project.py
    prompts.py
    api/
    host/
      backends/
        docker/
    kernel/
    sdk/
    tools/
  factory/
  evaluation/
  search/
```

Ownership:

- `core/`: leaf helpers, exceptions, versioning, patch parser.
- `contracts/`: Pydantic models and pure contract helper functions.
- `providers/`: provider protocols, local/replay/retry/failover adapters, hosted adapters, env and payload helpers.
- `learning/`: predictor model bank and observation extraction shared by runtime shell and search/evaluation.
- `tracing/`: provider-neutral trace identity, persistence, grouped materialization, and rendering.
- `storage/`: artifacts, run/session/factory-chat stores, state-store index/query infrastructure.
- `runtime/profile.py`, `runtime/loader.py`, `runtime/project.py`, `runtime/prompts.py`: runtime profile, runtime loading, baseline runtime initialization, and bundled prompt spec loading.
- `runtime/api/`: request loading, prompt-to-task compilation, execution-plan compilation, resume rebinding, result shaping, protocol builders.
- `runtime/host/`: public `RuntimeHost`, preflight, backend choice, local process dispatch, Docker dispatch, validation, finalization.
- `runtime/kernel/`: bundled solve-time runtime now in `task_runtime/`, plus runtime shell, memory graph, and `TaskRuntime` facade.
- `runtime/sdk/`: runtime bundle builder and protocol entrypoint.
- `runtime/tools/`: tool registry, sandbox, executor, async process records, generated-tool validation.
- `factory/`: build-runtime pipeline, goal planning, export, factory chat/follow-ups.
- `evaluation/`: benchmark suites, verifiers, evaluator, scoring.
- `search/`: archive, evolution engine, mutators, crossover.

## Import Boundaries

| Package | May import | Must not import |
|---|---|---|
| `core` | stdlib, tiny leaf third-party helpers | project subsystems |
| `contracts` | stdlib, Pydantic, `core` | providers, factory, host, kernel, storage implementation, CLI |
| `providers` | contracts, core, tracing persistence | factory, runtime kernel, CLI |
| `learning` | contracts, core, numeric helpers | factory orchestration, host dispatch, CLI |
| `storage` | contracts, core, tracing contracts | factory orchestration, host dispatch, CLI |
| `tracing` | contracts, core, provider stringification helpers | factory internals, runtime kernel internals |
| `runtime.api` | contracts, provider base types, tracing identity, core | runtime host, factory, CLI |
| `runtime.kernel` | contracts, runtime.api, storage/run store, runtime.tools, learning, tracing identity | runtime host, factory, evaluation/search, CLI |
| `runtime.host` | contracts, runtime.api, runtime.sdk, runtime loader/project, storage, providers | factory, evaluation/search, runtime kernel internals except bundled entrypoints |
| `factory` | contracts, storage, providers, evaluation/search, runtime.host, runtime loader/project | runtime.kernel internals |
| `evaluation` | contracts, runtime.host, storage, providers, runtime.api, learning | factory orchestration, runtime.kernel internals |
| `search` | contracts, evaluation, providers, storage, runtime.host, learning | runtime.kernel internals |
| `cli` | public service/facade modules | private helper modules |

Factory, evaluation, and search must call runtime execution through `RuntimeHost` and `runtime/api`, not `runtime/kernel` internals.

## Migration Map

### Core, Contracts, And Shared Learning

| Current path | Target |
|---|---|
| `agintor/exceptions.py` | `agintor/core/exceptions.py` |
| `agintor/versioning.py` | `agintor/core/versioning.py` |
| `agintor/patches.py` | `agintor/core/patches.py` |
| `agintor/utils.py` | keep as `agintor/utils.py` for first pass |
| `agintor/schemas.py` | split into `agintor/contracts/*.py`; keep temporary `agintor/schemas.py` shim |
| `agintor/predictors.py` | `agintor/learning/predictors.py` |
| `agintor/trace_labeler.py` | `agintor/learning/observations.py` |

Required contract modules:

| Target module | Owns |
|---|---|
| `contracts/tracing.py` | `OpenAITraceContext`, `RuntimeEvent` |
| `contracts/providers.py` | `ModelRequest`, `ModelResponse`, provider role/plan/replay allocation |
| `contracts/factory.py` | goal/build/export/factory-chat models |
| `contracts/benchmarks.py` | benchmark task, verifier bundle/spec, objective/task score/suite evaluation |
| `contracts/execution.py` | operation, input binding, request file refs, plan origin/node, execution plan, capability helpers |
| `contracts/state.py` | runtime state, memory, environment, and persistence snapshots |
| `contracts/checkpoints.py` | checkpoint refs/envelopes, recovery attempts/failure kind |
| `contracts/sessions.py` | runtime session seed/identity/message |
| `contracts/runtime.py` | runtime/kernel/run manifests and runtime descriptor metadata |
| `contracts/branches.py` | branch budget/plan/state/result/publication/resume snapshots |
| `contracts/side_effects.py` | side-effect receipts and reconciliation records |
| `contracts/protocol.py` | solve/run/runtime request/response/batch/inspect/resume protocol models |
| `contracts/search.py` | archive, mutation, predictor, evaluation-stage, evolution-history contracts |

### Runtime API, Loader, Host, And Docker

| Current path | Target |
|---|---|
| `agintor/runtime_api.py` | split into `agintor/runtime/api/*.py`; keep temporary shim |
| `agintor/runtime_profile.py` | `agintor/runtime/profile.py` |
| `agintor/runtime_loader.py` | `agintor/runtime/loader.py` |
| `agintor/project.py` | `agintor/runtime/project.py` |
| `agintor/runtime_host.py` | split into `agintor/runtime/host/*.py`; keep temporary shim |
| `agintor/container_runtime.py` | split into `agintor/runtime/host/backends/docker/*.py`; keep temporary shim |
| `agintor/container_entry.py` | `agintor/runtime/host/backends/docker/entrypoint.py` |

Required `runtime/api/` split:

| Target module | Owns |
|---|---|
| `context.py` | `PromptCompilation`, `AgentFrame`, `RuntimeBudget`, `RuntimeState`, `PolicyContext` |
| `request_loading.py` | `load_solve_request`, prompt/file/path extraction, benchmark task to solve request |
| `prompt_intent.py` | prompt classification and number/symbol/path/url/service extraction |
| `capabilities.py` | execution-plan provider/network/filesystem/service requirement helpers |
| `tracing.py` | runtime trace context builders, benchmark episode fields, evaluation unit keys |
| `plan_nodes.py` | operation node payloads, dependency helpers, branch/merge/verify node construction |
| `plan_compiler.py` | solve request to task and task/request to execution-plan compilation |
| `resume.py` | checkpoint rebinding, checkpoint to solve request, resume task/plan extraction |
| `results.py` | grouped run-result reduction, solve-result conversion |
| `protocol.py` | runtime solve/batch/inspect request builders and blocked episode synthesis |
| `failures.py` | runtime solve failure response shaping |

Promote cross-file private helpers before moving them. Example: replace imports of `_compile_request_file_ref` with a public `compile_request_file_ref`.

Required `runtime/host/` split:

| Target module | Owns |
|---|---|
| `host.py` | `RuntimeHost` public facade |
| `backend_selection.py` | local/docker backend choice |
| `preflight.py` | solve/batch/resume preflight checks |
| `resume_resolution.py` | checkpoint/run/session resume resolution |
| `finalization.py` | run/attempt finalization and failure shaping |
| `validation.py` | runtime response validation |
| `local_process.py` | local subprocess dispatch |

Required Docker split:

| Target module | Owns |
|---|---|
| `executor.py` | Docker executor facade |
| `image.py` | image digest/build/ensure logic |
| `commands.py` | Docker argv, mounts, environment |
| `path_mapping.py` | host/container path conversion |
| `request_rewrite.py` | request containerization |
| `checkpoint_rewrite.py` | checkpoint/open-handle/shell-state path rewrite |
| `run_rewrite.py` | run/attempt/side-effect/state payload rewrite |
| `response_rewrite.py` | response path and trace/checkpoint ref rewrite |

### Runtime Kernel, SDK, And Tools

| Current path | Target |
|---|---|
| `agintor/runner.py` | `agintor/runtime/kernel/facade.py`; keep shim exporting `TaskRuntime` |
| `agintor/task_runtime/*` | `agintor/runtime/kernel/*`; keep old package shims during migration |
| `agintor/shell.py` | `agintor/runtime/kernel/shell.py` |
| `agintor/memory_graph.py` | `agintor/runtime/kernel/memory_graph.py` |
| `agintor/runtime_sdk/*` | `agintor/runtime/sdk/*`; keep old package shims |
| `agintor/runtime_sdk/runtime_entry.py` | `agintor/runtime/sdk/entrypoint.py`, `handlers.py`, `failures.py` |
| `agintor/tool_runtime.py` | split into `agintor/runtime/tools/*.py`; keep temporary shim |

Split only the large kernel files first:

| Current file | Target shape |
|---|---|
| `task_runtime/branch_execution.py` | `runtime/kernel/branches/*.py` |
| `task_runtime/checkpointing.py` | `runtime/kernel/checkpointing/*.py` |
| `task_runtime/bounded_io.py` | `runtime/kernel/io/*.py` |
| `task_runtime/execution_loop.py` | `runtime/kernel/loop.py`, `root_frame.py`, `progress.py` |

Suggested `runtime/tools/` split:

| Target module | Owns |
|---|---|
| `models.py` | registered tool and async process records |
| `registry.py` | tool registry |
| `sandbox.py` | sandbox manager |
| `safety.py` | safety guard |
| `executor.py` | tool executor |
| `validation.py` | generated-tool validation |

### Storage, Tracing, Providers

| Current path | Target |
|---|---|
| `agintor/artifacts.py` | `agintor/storage/artifacts.py` |
| `agintor/run_store.py` | `agintor/storage/run_store.py` |
| `agintor/runtime_session_store.py` | `agintor/storage/runtime_session_store.py` |
| `agintor/factory_chat_store.py` | `agintor/storage/factory_chat_store.py` |
| `agintor/state_store.py` | split into `agintor/storage/state_store/*.py`; keep temporary shim |
| `agintor/openai_trace.py` | split into `agintor/tracing/*.py`; keep temporary shim |
| `agintor/providers.py` | convert to `agintor/providers/` package |
| `agintor/provider_common.py` | split across `agintor/providers/base.py`, `env.py`, `usage.py` |
| `agintor/provider_openai.py` | `agintor/providers/openai.py` |
| `agintor/provider_minimax.py` | `agintor/providers/minimax.py` |

Required `storage/state_store/` split:

| Target module | Owns |
|---|---|
| `layout.py` | filesystem/state DB layout |
| `connection.py` | DB open/init lifecycle |
| `schema.py` | SQLite DDL and schema/dirty marker |
| `store.py` | small `StateStore` facade |
| `indexers.py` | run/attempt/request/checkpoint/event/receipt/fingerprint/recovery indexing |
| `memory.py` | memory checkpoint shards and boundary snapshots |
| `rebuild.py` | rebuild from canonical records |
| `queries.py` | checkpoint, artifact, branch, recovery, retrieval, trace-status queries |
| `serializers.py` | JSON/path/payload coercion helpers |

`state_store.py` remains index/query infrastructure. `RunStore` canonical JSON remains the data authority.

Required `tracing/` split:

| Target module | Owns |
|---|---|
| `identity.py` | trace session IDs and factory/runtime/benchmark grouping keys |
| `layout.py` | trace directory layout |
| `persistence.py` | provider trace persistence; keep `persist_openai_trace` alias |
| `materialization.py` | grouped view rebuild/materialization state |
| `rendering.py` | Markdown and payload rendering in the first pass |

Required `providers/` split:

| Target module | Owns |
|---|---|
| `base.py` | provider protocol/base types |
| `local.py` | deterministic local provider |
| `replay.py` | `ReplayProvider` |
| `retry.py` | retry wrapper |
| `failover.py` | failover wrapper |
| `registry.py` | provider construction |
| `payloads.py` | provider payload serialization and path rewriting |
| `env.py` | provider env/API-key-file names |
| `usage.py` | pricing, usage, token accounting helpers |
| `openai.py` | OpenAI adapter |
| `minimax.py` | MiniMax adapter |

Preserve `from agintor.providers import build_provider` through `providers/__init__.py`.

### Factory, Evaluation, Search

| Current path | Target |
|---|---|
| `agintor/runtime_builder.py` | split into `agintor/factory/*.py`; keep temporary shim |
| `agintor/goal_rubric.py` | `agintor/factory/goals.py` |
| `agintor/prompt_builder.py` | `agintor/factory/prompt_builder.py` |
| `agintor/prompts.py` | `agintor/runtime/prompts.py` |
| `agintor/benchmarks.py` | `agintor/evaluation/benchmarks.py` |
| `agintor/verifiers.py` | `agintor/evaluation/verifiers.py` |
| `agintor/scoring.py` | `agintor/evaluation/scoring.py` |
| `agintor/evaluator.py` | `agintor/evaluation/evaluator.py` plus stage helpers |
| `agintor/archive.py` | `agintor/search/archive.py` |
| `agintor/evolution.py` | `agintor/search/engine.py` |
| `agintor/mutator.py` | `agintor/search/mutators.py` |
| `agintor/crossover.py` | `agintor/search/crossover.py` |

Required `factory/` split:

| Target module | Owns |
|---|---|
| `service.py` | public build/follow-up/apply entrypoints |
| `pipeline.py` | `_run_factory_pipeline` orchestration |
| `workspace.py` | build workspace layout/staging |
| `planning.py` | benchmark plan, verifier bundle, deployment/runtime plan, provider refinement |
| `export.py` | seed runtime copy, destination replacement, export validation |
| `followups.py` | factory chat follow-up validation/replacement |
| `trace_context.py` | factory trace context stamping |

### CLI, Templates, Tests

| Area | First-pass decision |
|---|---|
| `agintor/cli.py` | keep in place; update imports only |
| `agintor/templates/prompts/*.json` | keep in place |
| `agintor/templates/baseline_runtime/*` | keep in place; do not treat ignored/generated copies as source |
| `tests/*.py` | update imports and add import-boundary smoke tests |
| `pyproject.toml` | keep `where = ["."]`; update package data only if tracked package data moves |

## Compatibility Shims

Temporary shims allowed:

```text
agintor.schemas
agintor.runtime_api
agintor.runtime_builder
agintor.runtime_host
agintor.container_runtime
agintor.state_store
agintor.openai_trace
agintor.tool_runtime
agintor.provider_common
agintor.provider_openai
agintor.provider_minimax
agintor.runner
agintor.task_runtime.*
agintor.runtime_sdk.*
```

Rules:

- Shims re-export only.
- New internal imports use canonical target paths.
- Existing tests may keep old imports temporarily.
- Delete shims after internal imports and tests are canonical, except `agintor.schemas` may stay longer as a public compatibility surface.

## Runtime Bundle Gate

`agintor/runtime_sdk/bundle.py` is a migration gate.

The current bundle uses an explicit `_KERNEL_SOURCE_FILES` list. After moves:

- either update the allow-list after every runtime-side move, or
- replace it with a package-recursive collector that includes canonical runtime/kernel/api/contracts/providers/storage/tracing/learning/tool modules imported by `runtime/sdk/entrypoint.py`.

Validation must include a fresh bundled runtime import/inspect/solve smoke test. In-repo import success is not enough.

## Implementation Order

### Phase 0 - Baseline Guardrails

1. Record `git status --short`.
2. Record current LOC and top import hubs.
3. Run focused baseline tests:
   - `.\.venv\Scripts\python -m pytest tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py tests/test_factory_chat.py tests/test_runtime_builder.py`
4. Add or update public import smoke tests.
5. Add an import-boundary test scaffold.

Do not move files in this phase.

### Phase 1 - Package Skeleton And Leaf Moves

1. Create target packages and `__init__.py` files.
2. Move leaf modules: exceptions, versioning, patch helper, runtime profile if ready.
3. Keep old-path shims.
4. Update only necessary imports.
5. Run focused tests.

### Phase 2 - Contracts

1. Split `schemas.py` into `contracts/*.py`.
2. Keep `agintor/schemas.py` as a re-export shim.
3. Avoid rewriting every importer until later unless clarity demands it.
4. Run import smoke, runtime, evaluation, and factory tests.

### Phase 3 - Runtime API

1. Split `runtime_api.py` into `runtime/api/*.py`.
2. Promote private helpers used outside the module.
3. Keep `agintor/runtime_api.py` as a re-export shim.
4. Update host, Docker, SDK entrypoint, kernel, CLI, and tests where it reduces ambiguity.
5. Run runtime execution, host, container, sessions, and trace topology tests.

### Phase 4 - Shared Support

1. Convert provider modules into `providers/`.
2. Move predictor code into `learning/`.
3. Split `openai_trace.py` into `tracing/`.
4. Move durable stores into `storage/`.
5. Split `state_store.py`.
6. Run durability, trace topology, runtime session, and runtime execution tests.

### Phase 5 - Runtime Host And Docker

1. Split `runtime_host.py`.
2. Split `container_runtime.py`.
3. Preserve `RuntimeHost` and `DockerRuntimeExecutor` facades.
4. Run runtime host/container/session tests.
5. Run Docker-marked tests if available and affordable.

### Phase 6 - Runtime Kernel, SDK, Tools

1. Move `task_runtime/` to `runtime/kernel/`.
2. Move `runner.py`, `shell.py`, `memory_graph.py`, and `runtime_sdk/`.
3. Split only the large kernel files listed above.
4. Split `tool_runtime.py` after kernel imports settle.
5. Update bundle generation.
6. Validate a fresh bundled runtime.

### Phase 7 - Factory, Evaluation, Search

1. Split `runtime_builder.py` into factory service/pipeline/planning/export/follow-up modules.
2. Move `goal_rubric.py`, `project.py`, `prompt_builder.py`, and `prompts.py` to the targets above.
3. Move benchmarks/verifiers/scoring/evaluator into `evaluation/`.
4. Move archive/evolution/mutator/crossover into `search/`.
5. Verify evaluation/search still route through `RuntimeHost`.
6. Run factory chat, runtime builder, evaluation, and evolution tests.

### Phase 8 - Canonical Imports And Shim Removal

1. Search for old-path imports.
2. Replace internal imports with canonical package paths.
3. Keep only intentionally retained public shims.
4. Run import-boundary tests.
5. Run the default suite.

## Acceptance Criteria

The reorganization is done when:

- The default test suite passes.
- Focused runtime/host/container/session/factory/evaluation tests pass.
- A fresh bundled runtime imports and executes the runtime entrypoint.
- Internal imports no longer route through old flat-path shims.
- Factory/evaluation/search do not import `runtime.kernel` internals.
- `RuntimeHost` remains the public execution boundary for factory/evaluation/search.
- `contracts/` has no imports from implementation packages.
- No empty future packages or placeholder architecture files were added.
- No product behavior, checkpoint semantics, runtime protocol semantics, provider behavior, artifact roots, or benchmark scoring behavior changed intentionally.

## Deferred Follow-Up Commit

Only after green tests:

- Optional `src/` layout.
- Root docs/README cleanup.
- Historical workstream-doc organization.
- Scratch/generated file cleanup.
- Full test tree mirroring.
- Template path movement.
- Splitting `utils.py`.
- Deep CLI split.
- Removing retained public shims.
