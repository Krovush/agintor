# Mass Implement Audit: 20260408T061621Z-a939f049

- Repo root: `C:\Users\yaros\Desktop\Agintor MVP`
- Artifact root: `C:\Users\yaros\Desktop\Agintor MVP\Codex Mass Implement Output`
- Created at: `2026-04-08T06:16:21Z`
- Input digest: `c85744190bd65815e099b70bd82053e2beb91acaef834b121b91b093651cadb8`
- Worker count: `2`
- Merge policy: `mass-implement/v1`

## Ordering Constraints
- Preserve the frozen-planning contract before verifier freeze and leader validation consume normalized plans.
- Keep solve-time provider requirement handling at the host/runtime protocol boundary instead of leaking raw runtime exceptions.

## Validation Invariants
- Every family listed in family_targets must remain represented across the normalized train, val, and test pressure sets.
- Hosted-provider prompt solves must not be rejected when credentials are already present on the resolved provider object.
- Hosted-provider requirement misses must surface as structured contract failures instead of raw runtime tracebacks.

## Open Decisions
- Choose whether benchmark-plan normalization should fully rebuild uncovered partitions or minimally rehydrate missing family coverage from the local default plan.
- Choose whether prompt-mode hosted-provider dependency should be inferred from request shape, capability flags, or both.

## Workstreams
- `w1` `benchmark-normalization`: benchmark-plan normalization and frozen family coverage
  - owned_paths: agintor/runtime_builder.py, tests/test_runtime_builder.py
  - dependencies: none
- `w2` `runtime-provider-boundary`: host/runtime provider preflight and contract-shaped failure handling
  - owned_paths: agintor/runtime_host.py, agintor/runtime_api.py, agintor/runtime_sdk/runtime_entry.py, agintor/templates/baseline_runtime/memory_policy.py, tests/test_runtime_host.py
  - dependencies: none

## Worker Proposals
- `w1` `benchmark-normalization`: assigned
  - domain: benchmark-plan normalization and frozen family coverage
  - owned_paths: agintor/runtime_builder.py, tests/test_runtime_builder.py
  - summary: Add partition-aware benchmark-plan normalization so provider task-id payloads are treated as hints, not authority: restore missing target-family coverage from default train/val/test partition tasks, exclude synthetic train goal tasks from satisfying base family coverage, and append the exact synthetic train set back after normalization. Add regressions for a partial top-only payload on a top+mem goal and for preserving coverage-complete partial hints.
  - proposal: update agintor/runtime_builder.py
  - proposal: update tests/test_runtime_builder.py
- `w2` `runtime-provider-boundary`: ingested
  - domain: host/runtime provider preflight and contract-shaped failure handling
  - owned_paths: agintor/runtime_host.py, agintor/runtime_api.py, agintor/runtime_sdk/runtime_entry.py, agintor/templates/baseline_runtime/memory_policy.py, tests/test_runtime_host.py
  - summary: Tighten hosted-provider solve preflight around the resolved provider object and prompt-mode runtime-owned side paths, then make runtime-side provider/configuration failures return a protocol-shaped failed solve response instead of bubbling a raw traceback. The regression set updates host tests to cover explicit key-file credentials and the stricter prompt-mode gate.
  - proposal: update agintor/runtime_host.py
  - proposal: update agintor/runtime_api.py
  - proposal: update agintor/runtime_sdk/runtime_entry.py
  - proposal: update tests/test_runtime_host.py

## Merge Plan
- accepted_ops: 0
- manual_conflicts: 0
- stale_ops: 0
