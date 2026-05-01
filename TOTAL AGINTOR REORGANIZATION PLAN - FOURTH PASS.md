# Total Agintor Reorganization Plan - Fourth Pass

## Role Of This Plan

Read this after:

1. `TOTAL AGINTOR REORGANIZATION PLAN - REFINED.md`
2. `TOTAL AGINTOR REORGANIZATION PLAN - ALL THE WAY.md`
3. `TOTAL AGINTOR REORGANIZATION PLAN - THIRD PASS.md`

The third pass appears to have completed the real owner-module split. This fourth pass is not another broad reorganization. It is a closeout pass: verify the remaining facade surfaces, harden boundary tests against disguised wrappers, and produce commit-ready validation evidence.

Do not restart the reorg. Do not reintroduce old flat modules. Do not move root docs, workstream docs, prompt templates, or generated files. Do not change product behavior.

## Current Verified State

The pass-three claim is mostly supported:

- All required owner modules from the third-pass plan now exist.
- Old top-level modules are absent from the working tree.
- `agintor/` contains only `__init__.py`, `cli.py`, `utils.py`, package directories, and templates.
- `compileall` passes.
- `tests/test_import_boundaries.py` passes: `10 passed`.
- Runtime/host/container/session slice passes: `210 passed`.
- Factory/runtime-builder/durability/trace slice passes: `60 passed`.
- Fresh local runtime bundle smoke passes.
- Runtime bundle inspection reports no old-path references.

The agent did not broadly lie. The remaining issue is narrower: a few small facade or pass-through modules still need an explicit final-state decision, and the boundary test should catch those patterns instead of only catching re-export-only wrappers.

## Remaining Risk

`tests/test_import_boundaries.py` now checks required owner modules and monolith cleanup, but it does not fully classify pass-through subclass facades.

Current pass-through classes:

```text
agintor/runtime/kernel/bounded_io.py          BoundedIOMixin(_BoundedIOMixin): pass
agintor/runtime/kernel/branch_execution.py    BranchExecutionMixin(_BranchExecutionMixin): pass
agintor/runtime/kernel/execution_loop.py      ExecutionLoopMixin(...): pass
agintor/storage/state_store/layout.py         StateStoreError(...): pass
```

`StateStoreError` is a normal exception class and is fine.

The three runtime-kernel modules are tiny import-stability facades. They may be acceptable, but they must be either:

- removed after updating imports to the true owner modules, or
- explicitly retained as intentional public/kernel compatibility surfaces and guarded by tests.

Also verify the larger public facades are really facade-level:

```text
agintor/runtime/host/host.py                         RuntimeHost public facade
agintor/runtime/host/backends/docker/executor.py     DockerRuntimeExecutor public facade
agintor/runtime/kernel/facade.py                     TaskRuntime/import-stability facade
```

These can remain if they only orchestrate high-level public behavior and delegate implementation to owner modules.

## Required Outcome

The fourth pass is complete when:

- Every remaining facade is explicitly justified or deleted.
- Boundary tests reject accidental pass-through wrapper modules.
- Boundary tests allow only the intentionally retained facade modules/classes.
- No old flat modules or deleted packages exist.
- No source or test imports use old paths.
- Runtime bundles include canonical owner modules and no old paths.
- Focused suites, full suite, bundle smoke, and `git diff --check` pass or failures are clearly explained as environmental.

## Tasks

### 1. Classify Remaining Facades

Inspect:

```text
agintor/runtime/kernel/facade.py
agintor/runtime/kernel/bounded_io.py
agintor/runtime/kernel/branch_execution.py
agintor/runtime/kernel/execution_loop.py
agintor/runtime/host/host.py
agintor/runtime/host/backends/docker/executor.py
```

For each one:

- Keep it only if it is a real public/import-stability boundary.
- Delete it if all internal callers can use the true owner modules directly and no public/bundle reason remains.
- If retained, make the intent enforceable in `tests/test_import_boundaries.py`.

Do not add explanatory comments unless the intent is otherwise non-obvious. Prefer tests and final handoff over prose inside source files.

### 2. Harden Boundary Tests

Update `tests/test_import_boundaries.py` to detect pass-through subclass wrappers, not just import-only wrappers.

The test should:

- flag modules whose only body is `class X(Y): pass`, unless explicitly allow-listed;
- allow normal empty exception marker classes such as `StateStoreError`;
- keep enforcing required owner modules;
- keep enforcing focused-size limits for known former monoliths;
- keep rejecting deleted old paths in imports and text;
- keep rejecting forbidden cross-boundary imports;
- keep checking runtime bundle old-path patterns.

If a runtime-kernel facade remains, put it in an explicit allow-list named for intentional facades. Do not let it pass accidentally because it contains a trivial class body.

### 3. Review Large Focused Owners

Do a quick source review of large remaining owner modules:

```text
agintor/contracts/execution.py
agintor/runtime/api/plan_compiler.py
agintor/factory/planning.py
agintor/runtime/host/backends/docker/checkpoint_rewrite.py
agintor/runtime/kernel/branches/execution.py
agintor/storage/state_store/indexers.py
agintor/tracing/rendering.py
agintor/runtime/kernel/memory_graph.py
agintor/runtime/kernel/shell.py
```

Do not split them just because they are large. Split only if a clear refined-plan ownership boundary is still mixed inside the file. If they are cohesive owner modules, leave them and mention that in the final handoff.

### 4. Bundle Verification

Run a fresh bundle smoke:

```powershell
$base = Join-Path $env:TEMP ('agintor-fourth-pass-smoke-' + [guid]::NewGuid().ToString())
$runtime = Join-Path $base 'runtime'
$workspace = Join-Path $base 'workspace'
New-Item -ItemType Directory -Force -Path $base, $workspace | Out-Null
.\.venv\Scripts\python -m agintor.cli init-runtime $runtime
.\.venv\Scripts\python -m agintor.cli solve $runtime --prompt "Say hello from the fourth-pass canonical closeout smoke." --provider local --runtime-backend local --workspace $workspace
Get-ChildItem -Recurse -File $runtime | Select-String -Pattern "agintor\.runtime_api|agintor\.schemas|agintor\.task_runtime|agintor\.runtime_sdk|agintor\.runner|agintor\.tool_runtime|agintor\.openai_trace"
```

The final inspection must return no old-path references.

### 5. Final Validation

Run:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_import_boundaries.py
.\.venv\Scripts\python -m pytest tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py tests/test_runtime_sessions.py
.\.venv\Scripts\python -m pytest tests/test_factory_chat.py tests/test_runtime_builder.py tests/test_durability_contracts.py tests/test_trace_topology.py
.\.venv\Scripts\python -m pytest
git diff --check
```

Run Docker-marked tests only if Docker is available and stable:

```powershell
.\.venv\Scripts\python -m pytest -m docker
```

If Windows temp cleanup causes `WinError 5`, rerun the failing slice with a short explicit `--basetemp` outside the repo and record that in the handoff.

## Final Handoff

Include:

- Whether each remaining facade was retained or deleted.
- If retained, the product/import-stability reason.
- Boundary-test changes made.
- Large owner modules reviewed and whether any were further split.
- Validation commands and pass/fail results.
- Fresh bundle-smoke result.
- Confirmation that no product behavior was intentionally changed.
