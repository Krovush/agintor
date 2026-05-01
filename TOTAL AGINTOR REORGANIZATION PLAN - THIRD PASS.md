# Total Agintor Reorganization Plan - Third Pass

# **Do not change product behavior**

## Role Of This Plan

Read this after:

1. `TOTAL AGINTOR REORGANIZATION PLAN - REFINED.md`
2. `TOTAL AGINTOR REORGANIZATION PLAN - ALL THE WAY.md`

The refined plan remains the authority for target ownership and package boundaries. The all-the-way plan remains the authority for removing migration shims and old flat paths. This third-pass plan targets the current leftover state after the all-the-way pass: the old paths are mostly gone, but several canonical packages still hide large monoliths instead of implementing the refined owner-module split.

## Current State

Confirmed done:

- Old top-level modules were deleted from `agintor/`.
- `agintor/` now contains only `__init__.py`, `cli.py`, `utils.py`, package directories, and templates.
- Source and tests no longer import the deleted old paths.
- `compileall` passes.
- `tests/test_import_boundaries.py` passes.
- A fresh local runtime bundle smoke passed and did not report old-path references.

Confirmed not done:

- The refined owner-module split was only partially implemented.
- The current boundary test rejects old shims and re-export-only wrappers, but it does not prove the refined owner modules exist or own code.
- Several large files are still old monoliths under canonical names.

Large canonical monoliths still needing real ownership splits:

```text
agintor/contracts/models.py                         ~2050 LOC
agintor/runtime/api/plan_compiler.py                ~2617 LOC
agintor/runtime/host/host.py                        ~1134 LOC
agintor/runtime/host/backends/docker/executor.py    ~1849 LOC
agintor/factory/service.py                          ~1693 LOC
agintor/storage/state_store/store.py                ~1598 LOC
agintor/tracing/persistence.py                      ~1078 LOC
agintor/runtime/tools/executor.py                    ~864 LOC
```

Runtime kernel is also still mostly the old `task_runtime/` split under the canonical path:

```text
agintor/runtime/kernel/branch_execution.py
agintor/runtime/kernel/checkpointing.py
agintor/runtime/kernel/bounded_io.py
agintor/runtime/kernel/execution_loop.py
```

## Required Outcome

The third pass is complete when canonical ownership is real, not just path-level:

- The refined plan's required owner modules exist.
- Each required owner module contains the code it owns, not a re-export.
- The remaining facades are small and intentional.
- `contracts/models.py`, `runtime/api/plan_compiler.py`, `runtime/host/host.py`, Docker `executor.py`, `factory/service.py`, `storage/state_store/store.py`, `tracing/persistence.py`, and `runtime/tools/executor.py` are reduced to focused owners or facades.
- Runtime kernel large files are split into the refined branch/checkpoint/io/loop ownership shape.
- Import-boundary tests enforce owner-module existence and prevent monolith relapse.
- Behavior and public CLI/runtime semantics remain unchanged.

## Execution Order

Work in this order. Use the refined plan's tables for exact destination modules and names.

### 1. Contracts

Split `contracts/models.py` into the refined contract owner modules:

```text
contracts/tracing.py
contracts/providers.py
contracts/factory.py
contracts/benchmarks.py
contracts/execution.py
contracts/state.py
contracts/checkpoints.py
contracts/sessions.py
contracts/runtime.py
contracts/branches.py
contracts/side_effects.py
contracts/protocol.py
contracts/search.py
```

Keep `contracts/__init__.py` as aggregation only. Delete `contracts/models.py` unless there is a narrow reason to keep it as an internal implementation detail; if it remains, it must not own the entire contract surface.

Validation:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py tests/test_durability_contracts.py tests/test_runtime_execution.py tests/test_runtime_sessions.py
```

### 2. Runtime API

Split `runtime/api/plan_compiler.py` into the refined runtime API owner modules:

```text
runtime/api/context.py
runtime/api/request_loading.py
runtime/api/prompt_intent.py
runtime/api/capabilities.py
runtime/api/tracing.py
runtime/api/plan_nodes.py
runtime/api/plan_compiler.py
runtime/api/resume.py
runtime/api/results.py
runtime/api/protocol.py
runtime/api/failures.py
```

`plan_compiler.py` should keep plan compilation only. `runtime/api/__init__.py` remains aggregation only.

Validation:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py tests/test_trace_topology.py
```

### 3. Providers, Tracing, Storage, And Tools

These are independent enough to split in separate focused chunks.

Provider cleanup:

- Ensure `providers/registry.py` owns provider construction and cloning only.
- Move retry/failover/replay/payload/env/usage behavior into their refined modules if still folded into `registry.py`.
- Keep `providers/__init__.py` aggregation-only.

Tracing cleanup:

- Split `tracing/persistence.py` into identity, layout, persistence, materialization, and rendering owners.
- Keep `persist_openai_trace` as an API name if tests or callers use it; do not change trace semantics.

State-store cleanup:

- Split `storage/state_store/store.py` into layout, connection, schema, store facade, indexers, memory, rebuild, queries, and serializers.
- Preserve the existing data authority: `RunStore` canonical JSON remains authoritative; `StateStore` remains index/query infrastructure.

Tool-runtime cleanup:

- Split `runtime/tools/executor.py` into models, registry, sandbox, safety, executor, execution, and validation owners.

Validation:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py tests/test_trace_topology.py tests/test_durability_contracts.py tests/test_runtime_execution.py tests/test_runtime_sessions.py tests/test_runtime_builder.py tests/test_factory_chat.py
```

### 4. Runtime Host And Docker

Split host ownership:

```text
runtime/host/host.py
runtime/host/backend_selection.py
runtime/host/preflight.py
runtime/host/resume_resolution.py
runtime/host/finalization.py
runtime/host/validation.py
runtime/host/local_process.py
```

Split Docker ownership:

```text
runtime/host/backends/docker/executor.py
runtime/host/backends/docker/image.py
runtime/host/backends/docker/commands.py
runtime/host/backends/docker/path_mapping.py
runtime/host/backends/docker/request_rewrite.py
runtime/host/backends/docker/checkpoint_rewrite.py
runtime/host/backends/docker/run_rewrite.py
runtime/host/backends/docker/response_rewrite.py
```

Preserve `RuntimeHost` and `DockerRuntimeExecutor` as facades. Do not let host import factory/evaluation/search or runtime-kernel internals except the explicitly allowed runtime entrypoint surfaces.

Validation:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py
```

Run Docker-marked tests only if Docker is available and stable:

```powershell
.\.venv\Scripts\python -m pytest -m docker
```

### 5. Runtime Kernel

Finish the deeper kernel split from the refined plan:

- Move branch budget, provider allocation, branch execution, branch resume, cancellation, and result construction under `runtime/kernel/branches/`.
- Move checkpoint publication, envelope construction, resume loading, and checkpoint eligibility under `runtime/kernel/checkpointing/`.
- Move bounded IO, request-file handling, and artifact exposure under `runtime/kernel/io/`.
- Split execution-loop ownership into `loop.py`, `root_frame.py`, and `progress.py`.

Keep `TaskRuntime` as the public facade. Factory, evaluation, search, and host must still execute through `RuntimeHost` and protocol boundaries, not kernel internals.

Validation:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py
```

### 6. Factory

Split `factory/service.py` into refined factory owner modules:

```text
factory/service.py
factory/pipeline.py
factory/workspace.py
factory/planning.py
factory/export.py
factory/followups.py
factory/trace_context.py
factory/goals.py
factory/prompt_builder.py
```

Factory may use evaluation/search, storage, providers, runtime host, runtime loader/project, and public runtime API. It must not import runtime-kernel internals.

Validation:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py tests/test_factory_chat.py tests/test_runtime_builder.py
```

## Boundary Test Upgrade

Update `tests/test_import_boundaries.py` so it catches the current failure mode.

Add checks that:

- Required owner modules from the refined plan exist.
- Required owner modules are not re-export-only.
- The known monolith files are gone or below a focused-size threshold with an explicit allow-list reason.
- `contracts/models.py` does not own the full contract surface.
- `runtime/api/plan_compiler.py` does not own request loading, prompt intent, tracing helpers, resume, protocol builders, result shaping, and failure shaping.
- Runtime bundles do not include deleted old paths and do include the canonical owner modules.

Do not make the test brittle about exact line counts everywhere. Use it to enforce architecture shape, not cosmetic size.

## Final Verification

Run focused suites in slices first. If Windows temp cleanup causes `WinError 5`, rerun the failing slice with a short explicit `--basetemp` outside the repo.

Final commands:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py
.\.venv\Scripts\python -m pytest tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py
.\.venv\Scripts\python -m pytest tests/test_factory_chat.py tests/test_runtime_builder.py tests/test_durability_contracts.py tests/test_trace_topology.py
.\.venv\Scripts\python -m pytest
git diff --check
```

Fresh bundle smoke:

```powershell
$base = Join-Path $env:TEMP ('agintor-third-pass-smoke-' + [guid]::NewGuid().ToString())
$runtime = Join-Path $base 'runtime'
$workspace = Join-Path $base 'workspace'
New-Item -ItemType Directory -Force -Path $base, $workspace | Out-Null
.\.venv\Scripts\python -m agintor.cli init-runtime $runtime
.\.venv\Scripts\python -m agintor.cli solve $runtime --prompt "Say hello from the third-pass canonical ownership smoke." --provider local --runtime-backend local --workspace $workspace
Get-ChildItem -Recurse -File $runtime | Select-String -Pattern "agintor\.runtime_api|agintor\.schemas|agintor\.task_runtime|agintor\.runtime_sdk|agintor\.runner|agintor\.tool_runtime|agintor\.openai_trace"
```

The final inspection must return no old-path references.

## Final Handoff

Include:

- Which owner splits were completed.
- Any owner split intentionally deferred, with a concrete reason.
- Validation commands and pass/fail results.
- Confirmation that boundary tests now enforce owner-module existence and monolith cleanup.
- Confirmation that fresh runtime bundle smoke passed.
- Confirmation that no product behavior was intentionally changed.
