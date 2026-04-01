# Workstream 1: Factory, Planning, And Export

## Outcome

- `agintor build-runtime "<goal>" --destination <dir>` must freeze build-time planning artifacts before evolution starts, run bounded search, validate a leader, and export a runtime with a real deployment contract.
- `agintor solve <runtime_dir>` must support both benchmark mode and user-request mode against an exported runtime without re-entering the evolution path.
- The build workspace must become the canonical audit trail for the factory path: goal normalization, success criteria, benchmark plan, verifier bundle, runtime plan, factory profile, evolution history, and export outputs must all exist as reloadable artifacts on disk.
- Exported runtimes must contain runtime-only assets plus deployment metadata. Factory-only knobs, archive state, mutation history, and validation/test traces must stay outside the export.

## Boundaries

- Own the CLI build/export surface, build-time schemas, frozen planning artifacts, runtime-plan resolution, exported-runtime solve entrypoints, and deployment/export packaging.
- Keep benchmark and verifier richness bounded to what already exists in the repository. Workstream 4 owns expanding task families and grader ecosystems.
- Keep runtime orchestration, checkpoint/resume, and solve-time execution mechanics aligned with Workstream 2. This workstream owns the product surface and artifact contracts, not the scheduler internals.
- Keep long-term memory durability and promoted-tool asset materialization aligned with Workstreams 3 and 5. This workstream owns the export hooks and manifests for those assets, not the underlying subsystems.
- Defer signed provenance, artifact registry integration, capability negotiation, and migration/version-bridge work until the MVP export contract is stable.

## Current Baseline

- `agintor/cli.py` already exposes `init-runtime`, `solve`, `eval`, `evolve`, and `build-runtime`.
- `agintor/runtime_builder.py` already creates a seed runtime, goal-conditions the demo suite, runs bounded evolution, chooses a leader by goal score then validation, copies the leader to the destination, and writes `build_summary.json`.
- `agintor/runtime_loader.py` already enforces a runtime ABI string, computes runtime identity from mutable files plus immutable manifest inputs, and loads the four mutable policy modules.
- The export currently contains `runtime_manifest.json`, `runtime_profile.json`, `runtime_export_bundle.json`, `runtime_provenance_bundle.json`, and the four mutable policy files.
- Goal conditioning is still heuristic. `agintor/goal_rubric.py` extracts keywords, phrases, and target families, and `build_goal_conditioned_suite()` clones representative demo tasks with prompt emphasis.
- `agintor solve` is still benchmark-only. It requires `runtime_dir` plus `task_id` and does not yet accept a raw user prompt or request file.
- `runtime_profile.json` still mixes factory-side evaluation and evolution controls with runtime-side execution controls.

## Packaging And Provenance Decisions

- Keep Agintor itself as the installed Python CLI package.
- Keep exported runtimes as host-consumed runtime bundles, not standalone Python applications. The runtime is code plus manifests loaded by the Agintor host, so the export format should stay directory-first and inspectable.
- Add a deterministic archive wrapper only after the runtime directory contract is stable. The archive is a transport layer around the export, not the canonical in-repo representation.
- Keep the current hash-based provenance bundle as the MVP baseline. Signed provenance and stronger attestation belong after the export contract stops moving.

## Phase 1: Freeze The Build-Time Artifact Chain

- Add build-time schema objects for `GoalSpec`, `SuccessCriterion`, `SuccessCriteriaBundle`, `BenchmarkPlan`, `VerifierSpec`, `VerifierBundle`, `FactoryProfile`, `RuntimePlan`, `DeploymentContract`, and `BuildSummary`.
- Refactor `agintor/runtime_builder.py` into explicit stages: goal intake, goal normalization, success-criteria extraction, benchmark planning, verifier freeze, runtime planning, seed creation, evolution, leader validation, export.
- Persist the canonical workspace layout:

```text
workspace/
  goal/
    goal_spec.json
    success_criteria.json
  planning/
    benchmark_plan.json
    verifier_bundle.json
    runtime_plan.json
    factory_profile.json
  seed_runtime/
    ...
  evolution/
    evolution_history.json
  export/
    build_summary.json
    export_summary.json
```

- Make later stages consume the serialized artifacts they depend on rather than reopening the raw goal prompt and recomputing expectations.
- Keep MVP goal interpretation bounded and honest. It is acceptable for `GoalSpec` generation to remain heuristic at first, but it must record assumptions, deployment preferences, target families, and measurable success criteria instead of only keywords and phrases.

`Exit gate:` a successful `build-runtime` run produces the workspace layout above, and the build summary contains paths to the frozen goal, planning, and export artifacts.

## Phase 2: Separate Factory And Runtime Planning

- Split the current mixed `RuntimeProfile` into a logical `FactoryProfile` and `RuntimeProfile`.
- Keep backward compatibility in `agintor/runtime_profile.py` so existing embedded `runtime_profile.json` files still load, but make new builds write factory-only planning state to `planning/factory_profile.json` and runtime-only execution state to the exported runtime.
- Move evaluation thresholds, mutation controls, archive/search settings, and validation seed counts out of the exported runtime contract.
- Keep runtime execution budgets, topology thresholds, memory thresholds, tool thresholds, control thresholds, backend compatibility, and runtime provider mapping inside the runtime plan and exported runtime profile.
- Add an explicit provider plan that distinguishes the Agintor provider role from the runtime provider role.

`Exit gate:` changing factory-only settings no longer changes the exported runtime hash, and exported runtimes no longer carry mutation or archive configuration that only the factory uses.

## Phase 3: Add The Exported-Runtime User Solve Path

- Extend `agintor solve` so it supports:
  benchmark mode with `task_id` and suite selection,
  user-request mode with `--prompt` and `--prompt-file`.
- Introduce a bounded `SolveRequest` path that records prompt, context items, file paths, verification preference, allowed tool categories, and budget overrides.
- Add a `SolveResult` contract that reports the produced artifact, runtime hash, verification status, checks run, trace reference, budget usage, and faults.
- Keep the user-request adapter narrow. It should turn raw requests into supported bounded task envelopes rather than invent a second planning system inside solve.
- Make the CLI return structured JSON for both solve modes with a stable shape.

`Exit gate:` the same exported runtime works with both `agintor solve <runtime_dir> <task_id> --suite ...` and `agintor solve <runtime_dir> --prompt ...`, and prompt-mode output clearly states whether the result is verified or best-effort.

## Phase 4: Strengthen The Runtime Export Contract

- Add `deployment_contract.json` as a required export artifact.
- Populate the deployment contract with at least `entry_command`, `runtime_abi`, `python_version`, `supported_backends`, `required_env_names`, `network_policy`, `filesystem_policy`, and `notes`.
- Add load-time validation in `agintor/runtime_loader.py` for deployment-contract compatibility, not just ABI string equality.
- Add `export_summary.json` that ties together the runtime hash, code hash, source runtime, provider identities, deployment contract, export bundle, provenance bundle, and runtime profile.
- Export an inspectable runtime directory first. After that contract is stable, add a deterministic compressed archive as a transport form of the same export.
- Keep the exported runtime narrow: runtime manifest, runtime profile, mutable policies, deployment contract, export bundle, provenance bundle, and only the runtime-owned asset manifests that already exist.

`Exit gate:` a runtime exported from one workspace can be copied to a fresh machine with the Agintor host installed, loaded successfully, and rejected with clear contract errors when Python, backend, or required environment expectations are not met.

## Phase 5: Add Export Hooks For Durable Runtime Assets

- Add export-manifest placeholders for promoted tools, environment fingerprints, benchmark adapters, and memory snapshots only where those assets already exist as stable subsystem outputs.
- Keep the ownership split clean:
  Workstream 3 owns durable memory content and recovery semantics.
  Workstream 5 owns promoted-tool packaging, environment materialization, and provider/runtime environment details.
  Workstream 1 owns how those assets are referenced and bundled once they are real.
- Avoid inventing placeholder asset registries before the producing workstreams have stable formats.

`Exit gate:` export metadata can point to runtime-owned durable assets without forcing the factory archive, mutator traces, or benchmark-side internals into the runtime bundle.

## MVP Acceptance Sequence

1. `agintor build-runtime "<goal>" --destination <dir>` writes the frozen goal and planning artifacts, seed runtime, evolution outputs, export summary, deployment contract, export bundle, and provenance bundle.
2. `build_summary.json` includes at least `goal_spec_path`, `success_criteria_path`, `benchmark_plan_path`, `verifier_bundle_path`, `runtime_plan_path`, `workspace`, `output_runtime_dir`, `agintor_provider`, `runtime_provider`, `best_train_score`, `best_goal_score`, `best_val_score`, `archive_cells`, `accepted_mutations`, `export_bundle_file`, and `provenance_bundle_file`.
3. The exported runtime directory contains runtime-only assets plus deployment metadata and excludes factory-only planning state.
4. `agintor solve <runtime_dir> --prompt ...` works against the exported runtime and returns a structured `SolveResult`.
5. `agintor solve <runtime_dir> <task_id> --suite ...` continues to work in benchmark mode.

## File Ownership

- `agintor/cli.py`: command surface, argument parsing, dual-mode solve entrypoints, structured CLI payloads.
- `agintor/runtime_builder.py`: staged build pipeline, artifact persistence, export orchestration, build summary generation.
- `agintor/runtime_profile.py`: factory/runtime profile split, compatibility loading, serialization rules.
- `agintor/runtime_loader.py`: runtime ABI and deployment-contract validation, runtime identity, export compatibility checks.
- `agintor/runtime_api.py`: solve request/result contracts and runtime-facing entrypoints.
- `agintor/schemas.py` or a dedicated adjacent planning module: build-time schema definitions and export contracts.
- `agintor/templates/baseline_runtime/runtime_manifest.json`: immutable/mutable contract and runtime-owned file boundaries.
- `agintor/templates/baseline_runtime/runtime_profile.json`: runtime-only defaults after the profile split lands.

## Deferred Until Post-MVP

- Signed provenance and attestations.
- Artifact registry publication and retrieval.
- Forward/backward migration contracts across runtime ABI generations.
- Rich host capability negotiation beyond basic backend and Python-version checks.
- Sealed durable asset packaging for promoted tools and memory snapshots before those asset formats stabilize in their owning workstreams.
