# WS2 Fix Plan 02: Runtime Host Backend Dispatch

## Scope

This plan covers the request-selected backend dispatch bug in `agintor/runtime_host.py` across:

- `RuntimeHost.inspect()`
- `RuntimeHost.run_batch()`
- `RuntimeHost.solve()`
- `RuntimeHost.resume()`
- local launch helpers that currently inherit the host default backend instead of the selected execution backend

This plan does **not** implement the fix.

## Problem Statement

`RuntimeHost` already computes or persists a per-execution backend in several places, but the actual runtime transport still branches on the host object's constructor default (`self.runtime_backend`) instead of the normalized backend for the active execution unit.

That creates two WS2 contract violations:

1. A request that explicitly selects `docker` can still be inspected and launched as `local` if the host was constructed with `runtime_backend="local"`.
2. A host constructed with `runtime_backend="docker"` cannot honor a `local` solve/resume path because transport still goes through the Docker executor.

This is especially serious because WS2 says backend choice is part of the frozen runtime/deployment contract, and Docker is the path that must satisfy stronger isolation guarantees.

## Files and Functions Inspected

### Governing docs

- `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
- `PROJECT TARGET SPEC.md`
- `PROJECT PAPER.md`

### Runtime host / transport

- `agintor/runtime_host.py`
  - `RuntimeHost.__init__`
  - `RuntimeHost.inspect`
  - `RuntimeHost.run_batch`
  - `RuntimeHost.solve`
  - `RuntimeHost.resume`
  - `RuntimeHost._resolve_runtime_resume_request`
  - `RuntimeHost._run_local_inspect`
  - `RuntimeHost._run_local_batch`
  - `RuntimeHost._run_local_solve`
  - `RuntimeHost._run_local_resume`
  - `RuntimeHost._runtime_env`

### Backend validation / runtime entry

- `agintor/runtime_loader.py`
  - `_validate_deployment_contract`
  - `load_runtime`
- `agintor/runtime_sdk/runtime_entry.py`
  - `_run_batch`
  - inspect/solve/resume load paths
- `agintor/container_runtime.py`
  - `inspect`
  - `run_batch_protocol`
  - `solve_protocol`
  - `resume_protocol`

### Request builders / schemas

- `agintor/runtime_api.py`
  - `inspect_request_for_runtime`
  - `runtime_solve_request_for_task`
  - `runtime_solve_request_for_user_request`
  - `runtime_batch_request_for_tasks`

### Existing tests

- `tests/test_runtime_host.py`
- `tests/test_runtime_execution.py`
- `tests/test_container_runtime.py`

## Root Cause

The bug is architectural, not just three bad `if` statements.

### 1. Host default backend is being treated as the execution backend

In `runtime_host.py`, `self.runtime_backend` is currently doing three different jobs:

- host default / fallback backend
- inspect backend
- transport dispatch selector

Those are not the same concept. WS2 needs a per-execution backend, while `self.runtime_backend` should only be a default when the request/run manifest does not specify one.

### 2. `inspect()` is backend-sensitive but always uses the host default

`RuntimeHost.inspect()` always builds `InspectRequest(requested_backend=self.runtime_backend)` and dispatches using the same host default.

That is wrong because `runtime_entry.inspect` calls `load_runtime(..., runtime_backend=request.requested_backend)`, and `load_runtime()` / `_validate_deployment_contract()` compute backend-specific support and guarantees. So a solve or resume that should inspect as `docker` is currently inspected as `local` if the host default is local.

### 3. Solve / batch / resume persist the selected backend but do not use it for transport

The following methods compute or carry an effective backend:

- `run_batch()` computes `selected_backend`
- `solve()` computes `selected_backend`
- `resume()` resolves `runtime_request.runtime_backend`

But transport still branches on:

- `if self.runtime_backend == "docker" and self.container_executor is not None`

instead of the selected backend for the active execution unit.

### 4. Local subprocess launch leaks the host default backend

`_runtime_env()` always sets:

- `AGINTOR_RUNTIME_BACKEND = self.runtime_backend`

So even if local dispatch is the correct path, the runtime process still sees the host default backend instead of the selected backend.

### 5. Docker executor availability is tied to host construction, not request needs

`self.container_executor` is only created in `__init__` when the host itself was constructed with `runtime_backend="docker"`. A local-default host therefore has no way to honor a later docker-selected request.

### 6. Resume resolves the correct backend too late

`resume()` currently calls `inspect(runtime_dir)` before `_resolve_runtime_resume_request()`, even though the resume request/backend lives in the stored run manifest and reconstructed runtime request. That means resume inspects the runtime using the host default before it even knows which backend the resumed run is supposed to use.

## Required Invariants After the Fix

1. Every solve-time execution unit has exactly one effective backend.
2. The following must all use the same effective backend:
   - inspect request
   - deployment-contract validation
   - run manifest `runtime_backend`
   - runtime request payload
   - transport choice (`local` vs `docker`)
   - runtime process env (`AGINTOR_RUNTIME_BACKEND`)
3. `RuntimeHost.runtime_backend` is only a default/fallback, never the final transport selector once a request/run manifest provides a backend.
4. Batch execution remains homogeneous by backend for WS2. Mixed-backend batch requests must be rejected before launch rather than partially executed.
5. Resume must derive backend from the resumed run/checkpoint before inspect and preflight.

## Proposed Code Changes

### 1. Split "default backend" from "effective backend" in `RuntimeHost`

Keep `self.runtime_backend` as the host default only.

Add small backend helpers in `agintor/runtime_host.py`, for example:

- `_normalize_backend(value: str | None, fallback: str | None = None) -> str`
- `_selected_solve_backend(request: RuntimeSolveRequest) -> str`
- `_selected_resume_backend(runtime_request: RuntimeResumeRequest, manifest: RunManifest) -> str`
- `_selected_batch_backend(request: RuntimeBatchRequest) -> str`

These helpers should normalize casing/empties once and make the execution path use the same backend everywhere.

### 2. Make inspect backend-aware per call

Refactor `RuntimeHost.inspect()` so callers can provide the requested backend explicitly, while keeping the current default behavior for existing callers:

- `inspect(runtime_dir, requested_backend: str | None = None)`

Implementation:

- normalize `requested_backend or self.runtime_backend`
- build `InspectRequest` with that normalized backend
- dispatch to local or docker based on that normalized backend

This keeps `runtime_builder.py` and any existing `host.inspect(runtime_dir)` call sites working unchanged.

### 3. Add lazy Docker executor acquisition

Replace the constructor-only Docker executor assumption with a lazy helper, for example:

- `_docker_executor() -> DockerRuntimeExecutor`

Behavior:

- create and cache the executor on first docker-selected execution
- reuse it afterward

This lets a local-default host still honor a docker-selected request without mutating host construction semantics.

### 4. Fix `solve()` to use the selected backend end to end

In `RuntimeHost.solve()`:

- compute `selected_backend` first
- inspect with `requested_backend=selected_backend`
- normalize the copied runtime request so `request.runtime_backend == selected_backend`
- write manifests/bundles with `selected_backend`
- choose transport from `selected_backend`, not `self.runtime_backend`
- pass `selected_backend` into local env generation

No changes are expected in `runtime_entry` or `runtime_loader`; they already respect the request backend when given the right one.

### 5. Fix `resume()` ordering and dispatch

In `RuntimeHost.resume()`:

- resolve the runtime resume request first via `_resolve_runtime_resume_request()`
- derive the effective backend from the reconstructed request/run manifest
- then inspect with that backend
- then preflight and dispatch using that backend

This is the most important structural change because current ordering makes resume inspect with the wrong backend before the backend is even known.

### 6. Fix `run_batch()` backend normalization and transport

In `RuntimeHost.run_batch()`:

- derive one normalized batch backend from the request/invocations
- assert all invocations agree with `request.runtime_backend`
- inspect with that normalized batch backend
- use that backend for preflight and dispatch
- keep writing manifests with that same backend

WS2 should keep batch backend-homogeneous because `runtime_sdk/runtime_entry.py::_run_batch()` already rejects invocation/backend mismatch. The host should mirror that contract before launch instead of relying on runtime-side failure.

### 7. Thread the selected backend through local launch helpers

Update the local launch path so `_runtime_env()` receives the effective backend explicitly:

- `_runtime_env(runtime_dir, runtime_backend)`

Then update:

- `_run_local_inspect`
- `_run_local_batch`
- `_run_local_solve`
- `_run_local_resume`

to call `_runtime_env(..., runtime_backend=<selected_backend>)`.

This ensures the launched runtime process and any runtime-side backend-sensitive logic see the actual execution backend, not the host default.

## Test Plan

Add targeted tests in `tests/test_runtime_host.py`. Existing tests already cover many request/preflight/resume mechanics, but there is currently no focused coverage for backend override dispatch.

### 1. Solve uses request backend for inspect and transport

Case:

- host constructed with `runtime_backend="local"`
- solve request carries `runtime_backend="docker"`

Assert:

- inspect is performed for `docker`
- Docker transport is used
- local transport is not used
- run manifest records `runtime_backend="docker"`

### 2. Solve honors local request on a docker-default host

Case:

- host constructed with `runtime_backend="docker"`
- solve request carries `runtime_backend="local"`

Assert:

- inspect is performed for `local`
- local transport is used
- Docker transport is not used
- local env receives `AGINTOR_RUNTIME_BACKEND=local`

### 3. Resume derives backend from stored run/request before inspect

Case:

- host default differs from resumed run manifest backend

Assert:

- `_resolve_runtime_resume_request()` happens before backend-sensitive inspect/preflight
- inspect uses the resumed run backend
- dispatch uses the resumed run backend

### 4. Batch dispatch uses normalized request backend, not host default

Case:

- host default differs from the effective batch backend (can be exercised with monkeypatched request construction/helper)

Assert:

- inspect/preflight/transport all use the batch backend
- manifests preserve that same backend

### 5. Batch rejects mixed invocation backends before launch

Case:

- request runtime backend and invocation runtime backends disagree

Assert:

- host raises a contract/protocol error before local or docker transport starts

### 6. Optional direct helper/env unit tests

Add small unit coverage for:

- backend normalization helper
- `_runtime_env(..., runtime_backend=...)`
- lazy Docker executor creation/reuse

## Risks / Open Questions

### 1. `run_batch()` API symmetry

`run_batch()` currently does not expose a user-facing backend override parameter the way `solve()` does through `RuntimeSolveRequest`. For WS2, I would **not** expand the public API unless CLI or evaluator flows need it immediately. The internal fix still matters because:

- the code already computes/persists a selected backend
- resume depends on stored backend fidelity
- batch must stay internally consistent with its own request object

### 2. Inspect request IDs and hashing

`inspect()` and batch request IDs currently hash in `self.runtime_backend`. If we make inspect truly per-backend, those IDs may need to use the effective backend instead of the host default for better determinism. This is not the core bug, but it is worth aligning while touching the code.

### 3. Public `inspect()` compatibility

Adding an optional `requested_backend` parameter is low-risk as long as the default remains `self.runtime_backend`. Existing callers such as runtime export/build inspection should continue to work unchanged.

## Recommended Implementation Order

1. Add backend normalization + lazy docker executor helpers in `runtime_host.py`.
2. Refactor `inspect()` to accept an optional requested backend and dispatch on it.
3. Refactor `solve()` to inspect/preflight/dispatch using the selected backend.
4. Refactor `resume()` so backend resolution happens before inspect.
5. Refactor `run_batch()` to normalize one batch backend and reject mixed backends early.
6. Thread explicit backend into `_runtime_env()` and all local launch helpers.
7. Add focused host tests for solve, resume, batch, and env propagation.

## Expected Outcome

After this fix, backend selection becomes a true per-execution contract instead of a host-construction side effect. That restores the WS2 isolation model:

- local runs launch locally
- docker runs launch through Docker
- resume preserves the backend of the run being resumed
- manifests, capability exchange, preflight, and runtime process environment all agree on the same backend
