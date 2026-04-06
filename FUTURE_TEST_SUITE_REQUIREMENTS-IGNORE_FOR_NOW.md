# Test Suite Porting Requirements

## Build And Export

- `agintor build-runtime` must write a complete frozen workspace artifact chain covering goal normalization, planning, evolution, and export.
- Build leader selection must rank candidates by goal-conditioned score first and use validation only to break ties between equally goal-fit candidates.
- Leader selection must consider archived runtimes outside the current global-objective island when they have stronger goal-conditioned scores.
- Goal text canonicalization must not change goal-conditioned task matching or goal-score aggregation.
- Goal interpretation must not treat the word `build` as tooling pressure by itself.
- Goal-conditioned benchmark tasks must retain goal metadata, source-task provenance, target-family hints, and explicit goal emphasis in task prompts.
- Explicit runtime backend overrides must be frozen into goal constraints, deployment preferences, and runtime planning artifacts.
- Exported runtimes must exclude factory-only profile fields.
- Exported runtimes must exclude bytecode and cache artifacts.
- Exported runtimes must remain loadable after export and must fail closed on ABI mismatch, kernel tampering, or unsupported deployment backends.

## Runtime Identity And Host Contract

- Runtime identity must change when runtime-relevant profile values change.
- Runtime identity must change when bundled kernel content changes.
- Runtime identity must ignore factory-only profile changes.
- Runtime host inspection must report versioned runtime capabilities, including ABI, kernel version, storage schema version, and runtime SDK support.
- A bundled runtime must be executable using only its bundled runtime SDK.
- Runtime batch/container protocols must rewrite container workspace paths back to host paths in returned artifacts.
- Budget overrides must propagate from CLI and protocol requests into runtime execution.
- Evaluator caches must distinguish runtimes whose effective runtime profiles differ.

## CLI And Solve Modes

- `agintor init-runtime` must create a loadable runtime and optionally emit a demo suite.
- `agintor solve` must support benchmark mode and prompt-driven user-request mode.
- Prompt-mode solves must report `status`, `verified`, `best_effort`, and `verification_status` consistently with verifier outcomes.
- Prompt-mode adaptation must preserve Windows-style file-path queries across slash conventions.
- Tool-scope restrictions in prompt mode must prevent out-of-scope exact-tool paths and yield controlled failure when verification is required.
- CLI provider resolution must default to the exported runtime provider profile when the user does not override it.

## Workspace And Artifact Hygiene

- Implicit workspaces and artifact roots must resolve outside the repository by default.
- Explicit user-provided workspaces inside the repository must remain allowed.
- Successful runs with `artifact_mode=none` must leave no retained traces or checkpoints.
- Failed runs with `artifact_mode=on_failure` must retain diagnostic traces.
- Runs with `artifact_mode=always` must retain trace and checkpoint artifacts.
- Shared sandbox caches may persist reusable sandbox artifacts, but successful run workspaces must stay clean.
- Runtime-evaluator construction must be side-effect free.

## Shell And Runtime Invariants

- Canonical agents must never execute directly; clone-on-run is required and direct canonical execution must hard-invalidate.
- Horizontal workers must not share mutable solve state.
- Long-term memory must reset between independent tasks and persist only within transfer-scored episodes.
- Short-term summary replacement must preserve backlinks to raw evidence.
- Summaries and checkpoints must preserve artifact refs, open handles, unresolved goals, symbols, and resume constraints.
- Exact symbol and exact path retrieval must outrank fuzzy retrieval.
- Category-first tool discovery is mandatory.
- Tool hints must not bypass category-first discovery.
- Root tool scope filtering must occur after category discovery rather than before it.
- Verification-required tasks must return controlled failure when no verified terminal artifact is available.

## Tooling And Sandbox Behavior

- Generated expression tools must support safe named arguments, dependency-derived signatures, and deterministic replay validation.
- Unsafe generated tools must be rejected before side effects occur.
- Generated tools must materialize inside content-addressed sandbox directories.
- Sandbox hashes must change when runtime-relevant tool validation inputs change.
- Tool promotion must require safety validation, stability, pass-rate thresholds, and cross-task reuse thresholds.
- Backgroundable tools must dispatch asynchronously and update run statistics and distinct-task reuse state.
- Missing async process records and nonzero async exits must fail safely.
- Provider-backed tool synthesis must fall back cleanly when model output is malformed or incomplete.

## Evaluation And Verifiers

- Stage 0 must reject non-unique patches, oversized patches, immutable-file edits, and edits that escape mutable-method contracts.
- Staged evaluation must stop at the first failed stage.
- Stage 2 proxy evaluation must filter proxy tasks by touched scope and use a deterministic fallback when no scope-tagged proxy exists.
- Parent and child comparisons must use common random numbers on shared task subsets.
- Validation evaluation must use a five-seed window.
- Trace verifiers must support event-presence and event-count proxy checks.
- Exact verification failures in solve mode must be surfaced as unverified results rather than as verified success.

## Archive, Scheduler, And Evolution Accounting

- Quality-diversity archive replacement must prefer lower complexity when scores fall within the replacement delta.
- Archive descriptors must include interface-difference masks, behavior descriptors, scope tags, and complexity buckets.
- Scope scheduling must account for singleton credit, pairwise credit, objective-conditioned credit, and hard-failure tracking.
- Fully evaluated children must update scope credit even when they are rejected.
- Counterfactual scope credit must be computed from proxy reversions only for accepted children.
- Archive insertion count must be the source of accepted-progress accounting.
- Validation-phase coverage must count four-way joint scopes correctly.

## Benchmark Plugin And Suite Loading

- Benchmark suite loading must support registered in-process providers.
- Benchmark suite loading must support module-based plugin builders.
- Suite loading must reject unknown schema versions.
