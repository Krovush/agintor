# Total Agintor Reorganization Plan - All The Way

## Role Of This Plan

Read this after `TOTAL AGINTOR REORGANIZATION PLAN - REFINED.md`.

The refined plan is the authority for target package shape, ownership tables, import boundaries, migration maps, runtime-bundle requirements, and acceptance criteria. This plan is the execution brief for finishing the current uncommitted first-pass refactor all the way to that final state.

If the two plans seem to conflict, use this rule:

- Follow the refined plan for where code belongs.
- Follow this plan where it removes temporary shim allowances, wrapper-only modules, and migration-state compromises.

This remains a behavior-preserving source reorganization. Do not add WS4/WS5 features, redesign APIs, change runtime protocol semantics, change checkpoint semantics, change scoring, change provider behavior, change artifact roots, or change trace grouping.

## Current First-Pass State

The current uncommitted diff already did the broad package move:

- Created the target package tree: `core`, `contracts`, `providers`, `learning`, `tracing`, `storage`, `runtime`, `factory`, `evaluation`, and `search`.
- Moved many implementation files into canonical packages.
- Updated most internal source imports away from old flat paths.
- Added `tests/test_import_boundaries.py`.
- Changed runtime SDK bundling to recursively copy the package.

That is not done. It is a scaffold around several remaining monoliths and compatibility shims.

Confirmed unfinished areas:

- `agintor/contracts/__init__.py` still owns almost all contract definitions; `contracts/*.py` are mostly re-export slots.
- `agintor/runtime/api/_core.py` still owns almost all runtime API behavior; `runtime/api/*.py` are mostly re-export slots.
- `agintor/providers/__init__.py` still owns provider registry, replay, retry, failover, payload, and environment behavior; several provider modules are re-export slots.
- `agintor/runtime/host/host.py` is still the host monolith; `runtime/host/*.py` are mostly re-export slots.
- `agintor/runtime/host/backends/docker/executor.py` is still the Docker monolith; Docker split modules are mostly re-export slots.
- `agintor/storage/state_store/store.py` is still the state-store monolith; `storage/state_store/*.py` are mostly re-export slots.
- `agintor/tracing/persistence.py` is still the tracing monolith; `tracing/*.py` are mostly re-export slots.
- `agintor/runtime/tools/executor.py` is still the tool-runtime monolith; `runtime/tools/*.py` are mostly re-export slots.
- `agintor/factory/service.py` is still the factory-builder monolith; several factory modules are re-export slots.
- `agintor/runtime/kernel/` is mostly a one-to-one move of old `task_runtime/`; the large kernel files still need the deeper split specified in the refined plan.
- Old flat modules and old packages remain as `sys.modules` alias shims.
- Tests still import old paths heavily and monkeypatch old module strings.
- `tests/test_import_boundaries.py` currently allow-lists old shims instead of enforcing the final architecture.
- Recursive runtime bundling can still copy old shim paths until those paths are deleted.

## Final-State Standard

The finished repo should look like it was built with the refined package structure from the start.

Required:

- Implementation bodies live in canonical owner modules.
- Package `__init__.py` files aggregate public names only.
- No `_core.py` or package `__init__.py` acts as a hidden monolith.
- Re-export-only canonical modules are either filled with real ownership or deleted.
- Old flat modules and old compatibility packages are deleted unless there is a product-level reason to keep a real public facade.
- Source imports, tests, and monkeypatch strings use canonical paths.
- Runtime bundles contain canonical source paths only.
- Import-boundary tests fail if wrapper/shim architecture returns.

Keep in place:

- `agintor/cli.py`
- `agintor/utils.py`
- tracked prompt templates
- ignored/generated baseline runtime template files

Do not move root docs, workstream docs, prompt templates, generated scratch paths, or unrelated files in this source-reorg commit.

## Working Rule

For each subsystem:

1. Use the refined plan's ownership table for the exact target module split.
2. Move code bodies into those owner modules.
3. Keep local private helpers private; promote helpers only when another owner legitimately needs them.
4. Update source imports, tests, and monkeypatch strings to canonical paths.
5. Delete old shims and empty wrappers once callers are canonical.
6. Strengthen `tests/test_import_boundaries.py` for that subsystem.
7. Run focused validation before continuing.

Do not leave a file whose only purpose is to hide that code used to live somewhere else.

## Phase 0 - Baseline

Record current state:

```powershell
git status --short
git diff --stat
git ls-files --others --exclude-standard
```

Run:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py
.\.venv\Scripts\python -m pytest tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py tests/test_factory_chat.py tests/test_runtime_builder.py tests/test_durability_contracts.py tests/test_trace_topology.py
```

If the current first-pass refactor is already failing, fix the architectural cause before deeper moves.

## Phase 1 - Finish Ownership Splits

Work through these in dependency order. For each item, use the refined plan's required split tables instead of inventing a new layout.

1. `contracts`: move definitions out of `contracts/__init__.py`; delete `agintor/schemas.py` after source and tests use canonical contract imports.
2. `providers` and `learning`: move provider behavior out of `providers/__init__.py`; delete provider flat shims and learning flat shims after callers are canonical.
3. `tracing` and `storage`: split `tracing/persistence.py` and `storage/state_store/store.py`; preserve `RunStore` canonical JSON as data authority and `StateStore` as index/query infrastructure; delete old tracing/storage flat shims.
4. `runtime.api`: split `runtime/api/_core.py`; delete `_core.py` and `agintor/runtime_api.py`.
5. `runtime.host` and Docker: split host and Docker monoliths; preserve `RuntimeHost` and `DockerRuntimeExecutor` as facades; delete old host/Docker flat shims.
6. `runtime.kernel`, `runtime.tools`, and `runtime.sdk`: complete the kernel/tool/SDK move; delete `runner.py`, `shell.py`, `memory_graph.py`, `tool_runtime.py`, `task_runtime/`, and `runtime_sdk/` after source and tests are canonical.
7. `factory`: split `factory/service.py`; delete factory/runtime-loader flat shims once callers use `agintor.factory.*` and `agintor.runtime.*`.
8. `evaluation` and `search`: these mostly own real code already; finish canonical imports, tests, and old shim deletion.

Run focused tests after each item using the relevant test slice from the refined plan. At minimum keep `tests/test_import_boundaries.py` in every slice.

## Phase 2 - Delete Migration Artifacts

After canonical imports are in place, remove every old flat module or old package that exists only for compatibility.

Expected deletions include the refined plan's temporary shim list plus the flat modules currently replaced by canonical packages:

```text
agintor/archive.py
agintor/artifacts.py
agintor/benchmarks.py
agintor/container_entry.py
agintor/container_runtime.py
agintor/crossover.py
agintor/evaluator.py
agintor/evolution.py
agintor/exceptions.py
agintor/factory_chat_store.py
agintor/goal_rubric.py
agintor/memory_graph.py
agintor/mutator.py
agintor/openai_trace.py
agintor/patches.py
agintor/predictors.py
agintor/project.py
agintor/prompt_builder.py
agintor/prompts.py
agintor/provider_common.py
agintor/provider_minimax.py
agintor/provider_openai.py
agintor/run_store.py
agintor/runner.py
agintor/runtime_api.py
agintor/runtime_builder.py
agintor/runtime_host.py
agintor/runtime_loader.py
agintor/runtime_profile.py
agintor/runtime_session_store.py
agintor/schemas.py
agintor/scoring.py
agintor/shell.py
agintor/state_store.py
agintor/tool_runtime.py
agintor/trace_labeler.py
agintor/verifiers.py
agintor/versioning.py
agintor/task_runtime/
agintor/runtime_sdk/
```

If any listed path remains, it needs a product-facing reason in the final handoff and it must own real facade behavior. Do not keep it just because old tests or imports are easier.

## Phase 3 - Final Import Boundaries

Turn `tests/test_import_boundaries.py` from a migration smoke test into a final-state guard:

- Reject internal imports from deleted flat paths.
- Reject test imports from deleted flat paths unless a test is explicitly verifying a retained public facade.
- Reject re-export-only non-`__init__.py` modules without an explicit allow-list reason.
- Reject implementation imports from `contracts/`.
- Reject factory/evaluation/search imports from `runtime.kernel`.
- Reject runtime-host imports from factory/evaluation/search or kernel internals, except runtime SDK/protocol entrypoint surfaces.
- Reject runtime bundle manifests or bundled source trees containing old flat paths, `task_runtime/`, or `runtime_sdk/`.

This search should return no source hits:

```powershell
rg -n "agintor\.(schemas|runtime_api|runtime_builder|runtime_host|container_runtime|state_store|openai_trace|tool_runtime|provider_common|provider_openai|provider_minimax|runner|task_runtime|runtime_sdk|archive|artifacts|benchmarks|crossover|evaluator|evolution|exceptions|factory_chat_store|goal_rubric|memory_graph|mutator|patches|predictors|project|prompt_builder|prompts|run_store|runtime_loader|runtime_profile|runtime_session_store|scoring|shell|trace_labeler|verifiers|versioning)" agintor tests
```

Test hits are allowed only for explicitly retained public facades.

## Phase 4 - Runtime Bundle Gate

After deleting old paths, recheck runtime bundling. Recursive copying is acceptable only if it copies the canonical source tree and excludes migration artifacts.

Run a fresh bundled runtime smoke:

```powershell
$base = Join-Path $env:TEMP ('agintor-final-reorg-smoke-' + [guid]::NewGuid().ToString())
$runtime = Join-Path $base 'runtime'
$workspace = Join-Path $base 'workspace'
New-Item -ItemType Directory -Force -Path $base, $workspace | Out-Null
.\.venv\Scripts\python -m agintor.cli init-runtime $runtime
.\.venv\Scripts\python -m agintor.cli solve $runtime --prompt "Say hello from the final canonical reorganization smoke." --provider local --runtime-backend local --workspace $workspace
```

Inspect the bundle:

```powershell
Get-ChildItem -Recurse -File $runtime | Select-String -Pattern "agintor\.runtime_api|agintor\.schemas|agintor\.task_runtime|agintor\.runtime_sdk|agintor\.runner|agintor\.tool_runtime|agintor\.openai_trace"
```

The inspection must return no old-path references.

## Final Verification

Run:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py
.\.venv\Scripts\python -m pytest tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py tests/test_factory_chat.py tests/test_runtime_builder.py tests/test_durability_contracts.py tests/test_trace_topology.py
.\.venv\Scripts\python -m pytest
git diff --check
```

Run Docker-marked tests if Docker is available and stable:

```powershell
.\.venv\Scripts\python -m pytest -m docker
```

## Final Handoff

Include:

- Validation commands run and pass/fail results.
- Any intentional public facades that remain, with one-line product reasons.
- Confirmation that old internal imports are gone.
- Confirmation that re-export-only canonical modules are gone or explicitly justified.
- Confirmation that fresh bundled runtime smoke passed.
- Confirmation that no product behavior was intentionally changed.

Do not call the reorganization complete while any required owner module is still only a wrapper around a monolith.
