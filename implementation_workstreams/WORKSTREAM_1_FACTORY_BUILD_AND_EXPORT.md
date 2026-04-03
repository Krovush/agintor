# Workstream 1: Factory, Planning, and Export

## Outcome

- `agintor build-runtime "<goal>" --destination <dir>` executes an artifact-first factory pipeline that freezes planning artifacts before evolution, validates a leader, and exports a self-describing runtime.
- Exported runtimes carry a bundled solve-time kernel, a versioned runtime contract, runtime-only profile data, deployment metadata, and runtime-owned assets. They do not depend on host-package implementation reach-through.
- `agintor solve` and `agintor eval` run through the same runtime protocol boundary that exported runtimes use instead of bypassing the export with host-side imports.
- Upstream planning stays bounded and inspectable. Goal interpretation is typed, assumption-bearing, and repairable through frozen diagnostics rather than open-ended replanning.

## Sequence Position

- This workstream must land first.
- Workstreams 2 through 5 assume that the runtime boundary, version axes, exported artifact shape, and artifact chain are already frozen.
- No later workstream should redesign the build path, export format, or host/runtime ownership split.

## Boundaries

- Own the factory-side build stages, build-time schemas, goal normalization, success-criteria extraction, benchmark planning, verifier freeze, runtime-plan resolution, profile splitting, export packaging, deployment contract, and the host/runtime protocol boundary.
- Own the CLI path for `build-runtime` and the export-first routing for `solve` and `eval`.
- Keep solve-time orchestration, branch scheduling, checkpoint storage internals, durable runtime-state stores, serious benchmark expansion, and per-tool sandbox mechanics outside this workstream.
- Keep the current repository spine. Do not replace Agintor with another framework or move factory logic into runtime modules.
- Do not use MCP as the internal host/runtime protocol. External tool interoperability can exist later at the tool boundary, but the runtime boundary remains Agintor-specific and minimal.

## Baseline

- `agintor/cli.py` already exposes `build-runtime`, `solve`, `eval`, `evolve`, and `init-runtime`.
- `agintor solve` already accepts `--prompt` and `--prompt-file`, but the solve path is still too tied to host-side execution details.
- `agintor/runtime_builder.py` already writes `goal_spec.json`, `success_criteria.json`, `benchmark_plan.json`, `verifier_bundle.json`, `factory_profile.json`, `runtime_plan.json`, `deployment_contract.json`, `export_summary.json`, and `build_summary.json`, but the stages are not yet enforced as the only legal build path.
- `agintor/runtime_profile.py` already contains partial helpers for separating factory-owned and runtime-owned payloads, but the split is not yet authoritative.
- `agintor/runtime_loader.py` still resolves immutable runtime dependencies out of the host package, which keeps exported runtimes host-dependent.
- Exports still behave more like thin policy bundles than fully bounded runtime products.

## Core Decisions

- Preserve the existing factory/host/runtime/policy split. The right move is contract completion, not architectural replacement.
- Freeze three separate version axes:
  - `runtime_abi`: request/response and host/runtime contract compatibility
  - `kernel_version`: bundled solve-time runtime kernel version
  - `storage_schema_version`: checkpoint and durable runtime-state schema compatibility
- Treat the build workspace as the canonical audit trail. Every stage writes a schema-validated artifact and later stages reload from disk rather than re-reading raw prompts or ambient objects.
- Add bounded contradiction handling. Downstream evidence may trigger repairs through frozen planning diagnostics and a replan contract, but later stages may not silently reparse the raw goal text.
- Bundle a vendored `runtime_sdk/` or equivalent solve-time kernel into every export. The host becomes a launcher and protocol client, not the hidden implementation of solve-time behavior.
- Use one runtime protocol with JSON envelopes. Transport may be stdio for local launches and mounted request/result files where container execution needs them, but the contract must stay identical.
- Require `inspect` capability exchange before solve, eval, or resume. The runtime reports supported backends, tool runtimes, checkpoint support, storage-schema compatibility, runtime-owned asset capabilities, and side-effect receipt support.
- Make runtime identity depend only on runtime-owned inputs. Factory-only knobs must not change exported runtime hash.

## Phase 1: Freeze the Build-Time Artifact Chain

- Refactor `agintor/runtime_builder.py` into explicit persisted stages:
  - `goal_intake`
  - `goal_normalization`
  - `success_criteria_extraction`
  - `benchmark_planning`
  - `verifier_freeze`
  - `runtime_planning`
  - `seed_runtime_materialization`
  - `evolution`
  - `leader_validation`
  - `export`
- Add or finalize typed build-time schemas for:
  - `GoalSpec`
  - `GoalAssumption`
  - `SuccessCriterion`
  - `SuccessCriteriaBundle`
  - `BenchmarkPlan`
  - `VerifierSpec`
  - `VerifierBundle`
  - `FactoryProfile`
  - `RuntimeProfile`
  - `ProviderPlan`
  - `RuntimePlan`
  - `DeploymentContract`
  - `BuildSummary`
  - `ExportSummary`
- Every artifact must carry:
  - artifact ID
  - schema version
  - content digest
  - creation stage
- Persist a stable workspace layout:

```text
workspace/
  goal/
    goal_spec.json
    success_criteria.json
  planning/
    assumption_register.json
    benchmark_plan.json
    verifier_bundle.json
    runtime_plan.json
    factory_profile.json
    deployment_contract.json
    planning_diagnostics.json
    replan_contract.json
  seed_runtime/
    ...
  evolution/
    evolution_history.json
    validation_history.json
    stage_failures.json
    leaderboard.json
  export/
    build_summary.json
    export_summary.json
```

## Phase 2: Make Goal Interpretation Typed and Repairable

- Replace loose heuristic planning with a bounded three-step goal compiler:
  - deterministic local normalizer first
  - optional provider-assisted structured planner second
  - local validator and normalizer third
- When a hosted provider participates, require schema-conformant structured outputs for:
  - `GoalSpec`
  - `SuccessCriteriaBundle`
  - `BenchmarkPlan`
- Record explicit assumptions and unsupported claims in:
  - `goal/goal_spec.json`
  - `goal/success_criteria.json`
  - `planning/assumption_register.json`
- Add `plan_consistency_check()` before seed runtime materialization. It must catch contradictions such as:
  - requested backend unsupported by deployment contract
  - task family requiring repo edits while runtime plan forbids file access
  - plan expecting network use while deployment contract forbids network
  - benchmark family lacking a compatible verifier
- Add bounded repair through:
  - `planning/planning_diagnostics.json`
  - `planning/replan_contract.json`
- Repairs must operate on frozen artifacts. They may revise downstream planning objects, but they may not reopen the raw goal prompt as a hidden alternative source of truth.

## Phase 3: Split Factory Planning from Runtime Planning

- Make the profile split authoritative:
  - `FactoryProfile`: evaluation thresholds, mutation controls, archive policy, validation budgets, benchmark-generation settings, leader-selection rules
  - `RuntimeProfile`: solve budgets, topology thresholds, memory thresholds, tooling thresholds, control thresholds, supported backends, runtime provider mapping
  - `ProviderPlan`: factory-provider role and runtime-provider role resolution
- Export only runtime-owned planning data.
- Update runtime hashing so it covers:
  - bundled kernel manifest
  - runtime manifest
  - runtime-only profile payload
  - mutable policy files
  - runtime-owned asset manifests
- Exclude from runtime identity:
  - archive state
  - mutation settings
  - validation seed counts
  - benchmark-generation parameters

## Phase 4: Establish a Real Host/Runtime Boundary

- Add a bundled solve-time package such as `agintor/runtime_sdk/` and copy it into every export.
- Replace host reach-through manifests with a bundled `kernel_manifest.json`.
- Restrict runtime loader resolution so immutable runtime paths are legal only inside:
  - the runtime directory
  - the bundled runtime kernel subtree
- Add a thin host launcher such as `agintor/runtime_host.py` that:
  - writes a request envelope
  - negotiates capabilities with `inspect`
  - validates `runtime_abi`, `kernel_version`, and `storage_schema_version`
  - launches the exported runtime entrypoint
  - reads the result envelope
  - transports request and result envelopes
- Keep the host responsible for building, evolving, evaluating, and exporting. Keep the runtime responsible for solve-time execution only.

## Phase 5: Route Solve Through the Exported Runtime

- Make `agintor solve` always go through the runtime protocol client.
- Route `agintor eval` through the same protocol client and exported runtime entrypoint. Evaluation wins that depend on hidden host-side execution paths do not count.
- Normalize both solve modes onto the same runtime contract:
  - benchmark mode: benchmark task to runtime request
  - user-request mode: bounded solve request to runtime request
- Add runtime-facing contracts for:
  - `SolveRequest`
  - `SolveResult`
  - `InspectRequest`
  - `ResumeRequest`
  - `CapabilityExchange`
  - `CheckpointReference`
- `SolveResult` must report at least:
  - `request_id`
  - `runtime_hash`
  - `mode`
  - `artifact`
  - `status`
  - `verification_status`
  - `checks`
  - `trace_ref`
  - `checkpoint_ref`
  - `budget`
  - `provider_usage`
  - `faults`
  - `recoverability`
- Prompt-mode solve must explicitly distinguish:
  - `verified`
  - `partially_checked`
  - `best_effort`

## Phase 6: Freeze the Export Contract

- Make these artifacts mandatory in every export:
  - `runtime_manifest.json`
  - `runtime_profile.json`
  - `kernel_manifest.json`
  - `deployment_contract.json`
  - `runtime_export_bundle.json`
  - `runtime_provenance_bundle.json`
  - `export_summary.json`
  - bundled runtime kernel payload
  - mutable policy files
- `deployment_contract.json` must include at least:
  - `entry_command`
  - `runtime_abi`
  - `kernel_version`
  - `storage_schema_version`
  - `python_version`
  - `supported_backends`
  - `required_env_names`
  - `environment_allowlist`
  - `filesystem_policy`
  - `network_policy`
  - `dependency_digest_set`
  - optional container image digest
  - `capability_flags`
- Allow references to runtime-owned assets only when those asset formats already exist. Do not invent empty placeholder registries.

## Regression Gates

- Extend `tests/test_runtime_builder.py`, `tests/test_prompt_mode.py`, `tests/test_runtime_identity.py`, and adjacent boundary tests to prove:
  - artifact reload between build stages
  - runtime-hash independence from factory-only knobs
  - fresh-environment load with no host-source reach-through
  - benchmark and prompt solve through the runtime boundary
  - `inspect` and compatibility-mismatch failure
  - export completeness and incompleteness detection
  - absence of factory-only internals in the exported runtime
- Cover both local and Docker launch paths through the same request and result envelopes.

## Handoff to Workstream 2

- Workstream 2 receives:
  - a self-contained exported runtime
  - a versioned host/runtime protocol
  - split factory and runtime profiles
  - a bundled runtime-kernel manifest
  - validated deployment metadata
  - an export-first solve and eval path
- Workstream 2 must treat those boundaries as fixed and build solve-time execution semantics inside them.

## Acceptance Gates

1. `build-runtime` writes the full frozen artifact chain and later stages reload those artifacts from disk.
2. Changing factory-only settings does not change exported runtime hash.
3. Exported runtimes no longer resolve solve-time kernel files out of the host package.
4. A runtime copied into a fresh environment can be launched through the host/runtime protocol and either runs or fails with a clear contract error.
5. `agintor solve <runtime_dir> --task-id ... --suite ...` and `agintor solve <runtime_dir> --prompt ...` both execute through the same protocol boundary.
6. `agintor eval` exercises the exported runtime through the same protocol client rather than a hidden direct-import execution path.
7. Build summaries and export summaries expose enough provenance to inspect the normalized goal, frozen benchmark plan, verifier bundle, runtime plan, deployment contract, and exported runtime identity from the workspace alone.

## File Ownership

- `agintor/cli.py`: `build-runtime` and export-first `solve` routing
- `agintor/runtime_builder.py`: staged artifact-first pipeline and export orchestration
- `agintor/runtime_profile.py`: authoritative factory/runtime profile split
- `agintor/runtime_loader.py`: runtime contract enforcement and loader boundary checks
- `agintor/runtime_api.py`: runtime protocol schemas and solve contracts
- `agintor/runtime_host.py`: host launcher and protocol client
- `agintor/runtime_sdk/`: bundled solve-time kernel payload
- `agintor/schemas.py` or adjacent planning modules: build-time artifact contracts
- `tests/test_runtime_builder.py`, `tests/test_prompt_mode.py`, `tests/test_runtime_identity.py`, `tests/test_runtime_spec.py`, and adjacent new tests: build-pipeline, export, and runtime-boundary regression gates

## Deferred

- Signed provenance and attestations
- Artifact registry publication
- Cross-version migration helpers beyond explicit fail-closed compatibility checks
- Rich capability negotiation beyond the contract fields needed for the MVP
