# WS2 Fix Plan 06: Docker Isolation Must Fail Closed on Contract Read Errors
A Composit of WS2_FIX_PLAN_01 - WS2_FIX_PLAN_06 plans.

## Scope

Worker 6 only. This plan covers the Docker isolation fail-open bug in `agintor/container_runtime.py` around `_requires_network_none` and the minimal supporting refactor needed to make Docker launch policy derive from the same typed deployment-contract semantics the rest of WS2 already uses.

## Problem Statement

`DockerRuntimeExecutor._requires_network_none()` currently reads `deployment_contract.json` with raw `json.loads(...)` and catches every exception:

- missing file -> returns `False`
- unreadable file -> returns `False`
- corrupt JSON -> returns `False`
- schema-invalid payload -> returns `False`

That `False` is passed directly into `_docker_run_argv(..., network_none=False)`, which omits `--network none` for Docker `inspect`, `solve`, `run-batch`, and `resume`.

This is a WS2 contract violation. The workstream explicitly requires Docker contract enforcement to fail closed, and Docker is the backend that must satisfy `network_disablement` when the deployment contract requires it.

## Governing Requirements I Inspected

### WS2

From `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`:

- Core decision: Docker contract enforcement must fail closed.
- Phase 6: runtime execution must either satisfy the isolation policy or fail before solve begins.
- Acceptance gate: Docker execution must enforce explicit policy and fail closed on unsupported guarantees.

### Project spec

From `PROJECT TARGET SPEC.md`:

- Section 13 requires support for `local` and `docker` runtime backends.
- Section 18 treats forbidden network access and boundary violations as non-negotiable failures.

### Project paper

From `PROJECT PAPER.md`:

- Section 3.3 marks forbidden network access as hard invalidation.
- Sections 13 and 16 reinforce deterministic boundary enforcement and disallow isolation drift.

## Files and Functions Inspected

- `agintor/container_runtime.py`
  - `DockerRuntimeExecutor._requires_network_none`
  - `DockerRuntimeExecutor._docker_run_argv`
  - `DockerRuntimeExecutor.inspect`
  - `DockerRuntimeExecutor.run_batch_protocol`
  - `DockerRuntimeExecutor.solve_protocol`
  - `DockerRuntimeExecutor.resume_protocol`
- `agintor/runtime_loader.py`
  - `_load_deployment_contract`
  - `_resolved_runtime_isolation_policy`
  - `_effective_guarantees_for_backend`
  - `_validate_deployment_contract`
- `agintor/runtime_builder.py`
  - `_build_deployment_contract`
- `agintor/project.py`
  - `_refresh_deployment_contract`
- `agintor/schemas.py`
  - `DeploymentContract`
  - `RuntimeIsolationPolicy`
- `tests/test_container_runtime.py`
- `tests/test_runtime_host.py`

## Current Behavior and Reproduction

Observed directly from the current code path:

- `_requires_network_none(missing_contract_runtime_dir) -> False`
- `_requires_network_none(corrupt_contract_runtime_dir) -> False`
- `_requires_network_none(valid_restricted_contract_runtime_dir) -> True`

So the current system does not distinguish:

- "the contract explicitly allows network access"
- from "the executor could not determine the contract at all"

That is the core fail-open bug.

## Root Cause

There are three architectural problems, not just one bad `except`:

1. `container_runtime.py` re-parses deployment policy independently from `runtime_loader.py` instead of using one typed source of truth.
2. `_requires_network_none()` returns a lossy boolean, so parse failure and policy evaluation are collapsed into the same branch.
3. The helper is invoked late as a convenience flag builder, not as an explicit pre-launch contract check.

The result is that Docker launch policy is decided by a best-effort JSON sniff instead of the WS2 deployment-contract loader path.

## Required Invariants After the Fix

1. Docker launch policy must be derived from a valid typed `DeploymentContract`, not ad hoc raw JSON access.
2. Missing, unreadable, corrupt, or schema-invalid deployment contracts must abort Docker launch before `docker run` starts.
3. `--network none` must be set whenever the resolved isolation policy requires `network_disablement` or resolves to `network_policy in {"none", "restricted"}`.
4. No Docker path may silently downgrade a required guarantee because a contract could not be parsed.
5. `inspect`, `solve`, `run-batch`, and `resume` must all use the same launch-policy resolution path.

## Proposed Fix

### 1. Replace the raw JSON sniff with a strict typed launch-policy loader

Refactor the current `_requires_network_none(runtime_dir)` helper into a strict helper that:

- loads the runtime deployment contract through shared typed logic
- resolves the effective `RuntimeIsolationPolicy`
- derives `network_none` from that policy
- raises a contract/load error on any read/parse/validation failure

Recommended shape:

- move shared contract-loading logic into a reusable helper owned by `agintor/runtime_loader.py`
- have `container_runtime.py` call that helper instead of `json.loads(...)`

Recommended ownership:

- `runtime_loader.py` already owns deployment-contract loading, isolation-policy resolution, and preflight validation
- `container_runtime.py` should consume that logic, not duplicate it

### 2. Make policy resolution explicit and pre-launch

Each Docker entrypoint should resolve launch policy before building argv:

- `inspect`
- `run_batch_protocol`
- `solve_protocol`
- `resume_protocol`

That pre-launch step should either:

- return a typed launch policy object or at least `network_none: bool`
- or raise immediately with a clear contract failure

The important semantic change is that failure to determine launch policy is itself a launch-blocking error.

### 3. Keep `_docker_run_argv()` pure

`_docker_run_argv()` is already a good low-level builder. Keep it pure:

- it receives a boolean `network_none`
- it does not do any file I/O
- all contract validation happens before this function is called

### 4. Surface a contract error, not a downgraded launch

The raised exception should clearly identify:

- the runtime path
- the deployment contract path
- whether the failure was missing file, unreadable file, corrupt JSON, or schema/validation failure

Recommended error class:

- `RuntimeLoadError` if practical, since this is a runtime contract/preflight failure rather than a subprocess failure

If reusing `RuntimeLoadError` would create awkward coupling, a local exception is acceptable, but the failure must still be explicit and pre-launch.

## Concrete Code Changes

### `agintor/runtime_loader.py`

Add or promote a shared helper for deployment-contract loading/resolution that `container_runtime.py` can call safely without reimplementing parsing rules.

Target outcome:

- one typed path for:
  - loading `DeploymentContract`
  - resolving `RuntimeIsolationPolicy`
  - deriving whether Docker must claim `network_disablement`

This can be done by:

- making the relevant helper public, or
- adding a new shared helper in `runtime_loader.py`

I do **not** recommend importing the current private `_load_deployment_contract` directly from `container_runtime.py` without first making the ownership explicit.

### `agintor/container_runtime.py`

Replace:

- `_requires_network_none(runtime_dir) -> bool`

With something equivalent to:

- `_resolve_docker_launch_policy(runtime_dir) -> <typed policy>`
  - strict contract load
  - strict isolation-policy resolution
  - `network_none` derivation
  - raises on failure

Then update these call sites to resolve launch policy before command construction:

- `inspect`
- `run_batch_protocol`
- `solve_protocol`
- `resume_protocol`

### `tests/test_container_runtime.py`

Add focused regression coverage for:

1. valid contract with `network_policy="restricted"` -> `network_none` is `True`
2. valid contract with `required_guarantees=["network_disablement"]` -> `network_none` is `True`
3. valid contract with provider-only / unrestricted policy -> `network_none` is `False`
4. missing `deployment_contract.json` -> strict helper raises
5. corrupt JSON contract -> strict helper raises
6. schema-invalid contract -> strict helper raises

Also add at least one launch-path test proving no Docker subprocess is started when contract resolution fails.

Best version:

- monkeypatch `subprocess.run`
- call one Docker entrypoint with a corrupt contract
- assert the contract error is raised before `subprocess.run` is invoked

If the shared helper is used by all four entrypoints, one path-level test plus helper-level tests is sufficient.

### `tests/test_runtime_host.py`

Optional but useful:

- add a host-level Docker test asserting the surfaced error remains a clear contract/preflight failure rather than a generic downgraded Docker run

This is secondary to the `container_runtime` regression tests.

## Test Plan

Minimum required regression suite:

1. strict launch-policy helper returns `network_none=True` for restricted/no-network contracts
2. strict launch-policy helper returns `network_none=False` only for valid contracts that actually permit network access
3. missing contract raises before Docker launch
4. corrupt contract raises before Docker launch
5. schema-invalid contract raises before Docker launch
6. at least one Docker entrypoint test verifies `subprocess.run` is not called on contract failure

## Risks and Open Questions

### Error-type choice

Preferred:

- use `RuntimeLoadError`

Why:

- this is a contract/preflight failure, not a runtime subprocess failure

### Shared-helper ownership

Preferred:

- keep shared contract-loading logic in `runtime_loader.py`

Why:

- that file already owns deployment-contract validation and backend guarantee reasoning
- it avoids a second source of truth

### Backward compatibility

Not a blocker for this repo. If a runtime has a malformed or unreadable contract, it should fail. WS2 requires fail-closed behavior, and this repository explicitly does not need to preserve broken legacy behavior.

## Recommended Implementation Order

1. Introduce the shared strict contract/policy loader in `runtime_loader.py`
2. Refactor `container_runtime.py` to consume it and remove the fail-open helper
3. Add helper-level regression tests
4. Add one Docker entrypoint regression proving no subprocess launch on contract failure
5. Optionally add one host-level propagation test if error clarity is still weak

## Definition of Done

This issue is fixed when all of the following are true:

- Docker never launches with network access merely because the contract could not be read
- contract-read/parse/validation failures abort launch before `docker run`
- `--network none` is driven by the same typed isolation policy used elsewhere in WS2
- tests cover both valid restricted contracts and malformed-contract failure paths

