# WS2 Fix Plan 03: Single-Output Verify Artifact Shape

## Scope

This plan covers only the single-output verify-path bug in the WS2 runtime execution flow.

Target issue:

- `agintor/runner.py` around `_execute_verify_node`
- Related terminal verification callsites in the same file that currently rebuild `{output_key: value}` payloads instead of using the runtime's canonical terminal-artifact shape

This is a fix-plan only. Do not implement here.

## Docs Inspected

- `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
  - Phase 3: one runtime-native `ExecutionPlan` contract
  - Runnable-node ordering and terminal verification flow
  - Acceptance gates around benchmark/prompt execution and verification behavior
- `PROJECT TARGET SPEC.md`
  - Section 7: verifier freeze and artifact-shape compatibility
  - Section 11: verification ladder and stopping contract
  - Section 18: non-negotiable invariant that tasks requiring verification must not terminate as complete without a verified terminal artifact
- `PROJECT PAPER.md`
  - Runtime verifier score definition
  - Control/verification/stopping rules for exact-verifier tasks

## Code And Tests Inspected

- `agintor/runner.py`
  - `_run_root_frame`
  - `_terminal_artifact`
  - `_execute_verify_node`
  - top-level run loop merge handling for `merge_vertical` and `merge_horizontal`
  - `_maybe_verify`
- `agintor/verifiers.py`
  - `verify_task_with_evidence`
  - `run_checker`
- `agintor/runtime_api.py`
  - `_append_verify_node`
  - `compile_execution_plan_from_task`
- `agintor/benchmarks.py`
  - existing `string_exact` / `number_exact` benchmark tasks
- `tests/test_runtime_execution.py`
  - current explicit verify-node coverage
  - current lack of single-output exact-verifier regression coverage

## Observed Failure Mode

### Direct reproduction

I reproduced the bug with two single-output benchmark tasks:

1. `number_exact` direct-response task returning `42`
2. `string_exact` direct-response task returning `"hello"`

Both were configured with:

- one terminal output key
- `verification_required=True`
- `allow_best_effort=False`
- exact verifier present

Observed runtime result in both cases:

- `artifact == {"error": "controlled_failure"}`
- `verifier_score == 0.0`
- runtime emitted verification checks and then failed the task

### Verifier-level confirmation

`agintor/verifiers.py` behaves correctly today:

- `verify_task_with_evidence(task, 42, []) -> 1.0`
- `verify_task_with_evidence(task, {"answer": 42}, []) -> 0.0`
- `verify_task_with_evidence(task, "hello", []) -> 1.0`
- `verify_task_with_evidence(task, {"answer": "hello"}, []) -> 0.0`

### Contrast case

A multi-output exact JSON task still verifies successfully. The failure is shape-specific, not a general verifier failure.

## Problem Statement

WS2 already has a canonical terminal-artifact rule in `TaskRuntime._terminal_artifact(...)`:

- one terminal output key -> return the raw value
- multiple terminal output keys -> return a `{output_key: value}` object

That rule is correct for exact scalar/string verification.

The bug is that terminal verification does not consistently use that canonical shape.

## Root Cause

### Primary root cause

`agintor/runner.py` has duplicated terminal-artifact assembly logic.

Correct path:

- `_terminal_artifact(plan, artifacts)` collapses single-output plans to the raw scalar/string

Incorrect paths:

- `_execute_verify_node(...)` always builds `{output_key: value}`
- merge-time verification in the main run loop for `merge_vertical`
- merge-time verification in the main run loop for `merge_horizontal`

As a result, the runtime sometimes verifies a singleton dict even though the canonical terminal artifact for the same plan is a raw scalar/string.

### Why this is the architectural bug

The issue is not in `string_exact` or `number_exact`.

Those verifiers are enforcing the intended contract: exact single-output tasks verify the raw terminal artifact. The broken ownership is in the runner, where multiple callsites are reconstructing terminal verification payloads independently instead of using one canonical artifact-shaping function.

## Required Invariant

For any runtime path that performs terminal verification:

- if the verified output set contains exactly one terminal output key, the verifier input must be the raw value stored for that key
- if the verified output set contains multiple terminal output keys, the verifier input must be a mapping keyed by output name

This invariant must hold identically for:

- explicit verify nodes
- non-node fallback terminal verification paths
- vertical merge verification
- horizontal merge verification

## Fix Direction

Do not loosen the verifiers to unwrap singleton dicts.

That would hide a broken runtime contract and create another silent artifact-shape path. The durable fix is to centralize terminal verification artifact shaping in the runner and make every verification callsite use it.

## Proposed Code Changes

### 1. Introduce one canonical helper for verification artifact shaping

In `agintor/runner.py`, replace ad hoc dict construction with one helper dedicated to output-key-based artifact shaping.

Recommended shape:

- either extend `_terminal_artifact(...)` to accept an optional `output_keys` parameter
- or add a nearby helper such as `_artifact_for_output_keys(output_keys, artifacts)`

Required behavior:

- `len(output_keys) == 1` -> return `artifacts[that_key]`
- otherwise -> return `{output_key: artifacts.get(output_key) for output_key in output_keys}`

Important:

- this helper should shape the verifier input only
- it should not mutate `state.artifacts`
- it should preserve the existing canonical raw-vs-object contract already implied by `_terminal_artifact(...)`

### 2. Update `_execute_verify_node(...)`

Current behavior:

- reads `terminal_output_keys` from node metadata
- always builds a mapping

Planned behavior:

- read `terminal_output_keys` from node metadata exactly as today
- pass those keys through the new canonical helper
- feed the resulting shaped artifact into `_maybe_verify(...)`

This is the direct fix for the reported bug.

### 3. Update merge-time verification paths to use the same helper

Affected callsites in `agintor/runner.py`:

- `merge_vertical`
- `merge_horizontal`

These paths currently rebuild dict payloads manually before calling `_maybe_verify(...)`.

They should use the same canonical helper so terminal verification stays consistent across:

- explicit verify-node execution
- merge-driven verification
- any future terminal verification path added to the runner

### 4. Keep verify-node status payload unchanged

The output of `_execute_verify_node(...)` should stay:

- `{"verifier_score": ..., "verified": ...}`

Only the verifier input artifact shape should change. No plan schema or result schema change is required.

### 5. Do not bump ABI or storage schema for this fix

This is a runner-side contract correction, not a serialized-protocol redesign.

Unless implementation uncovers a hidden persisted-contract dependency, this should remain:

- no `ExecutionPlan` schema change
- no runtime ABI bump
- no storage schema bump

## Test Plan

Add focused regression coverage in `tests/test_runtime_execution.py`.

### 1. Single-output `number_exact` regression

Create a one-operation task with:

- `output_key="answer"`
- `expected=42`
- `verifier_type="number_exact"`
- `verification_required=True`
- `allow_best_effort=False`

Run via `TaskRuntime.run_task(...)` with a replay response of `42`.

Assert:

- explicit verify node exists in the compiled plan
- `result.verifier_score == 1.0`
- `result.artifact == 42`
- `result.artifact` is not a singleton dict wrapper
- terminal outcome is not `{"error": "controlled_failure"}`

### 2. Single-output `string_exact` regression

Same structure, with:

- `expected="hello"`
- `verifier_type="string_exact"`

Run with replay response `"hello"`.

Assert:

- `result.verifier_score == 1.0`
- `result.artifact == "hello"`
- no controlled failure

### 3. Multi-output regression guard

Keep or extend an existing multi-output exact-verifier test so we still prove:

- multi-output artifacts remain keyed objects
- multi-output verification still passes

This protects against overcorrecting toward raw scalars everywhere.

### 4. Optional helper-level unit test

If a new helper is introduced, add a narrow unit test for artifact shaping:

- one key -> raw value
- multiple keys -> mapping

This is useful because it protects the invariant directly without needing complex orchestration setup.

## Risks / Open Questions

### Risk: hidden singleton-dict assumptions

If any trace assertions, short-term artifact nodes, or downstream inspection code implicitly relied on singleton dict payloads for verification, those expectations will need to be updated.

That said, those expectations would be downstream of already-broken runtime behavior, so they should not override the canonical contract.

### Open question: should `artifact_contract` encode the terminal shape explicitly?

Probably not required for the WS2 fix.

Current evidence says the runtime already has the intended source of truth in `_terminal_artifact(...)`; the bug is inconsistent use, not missing schema. If future work needs external verifier consumers to reason about shape without runner code, then adding an explicit `artifact_shape` field can be considered later. It should not block this fix.

## Recommended Implementation Order

1. Add the canonical artifact-shaping helper.
2. Switch `_execute_verify_node(...)` to that helper.
3. Switch merge-time verification callsites to the same helper.
4. Add the two single-output regression tests.
5. Re-run the existing runtime execution suite, especially explicit verify-node coverage and multi-output verification coverage.

## Definition Of Done

This issue is fixed when all of the following are true:

- single-output `number_exact` tasks verify successfully
- single-output `string_exact` tasks verify successfully
- the verifier input shape is canonical across all terminal verification paths
- multi-output exact verification remains unchanged
- no ABI or storage version bump is introduced for this correction
