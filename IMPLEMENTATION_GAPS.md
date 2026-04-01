# Implementation Gaps

This document is the current-state crosswalk between:

- `PROJECT PAPER.md`
- `PROJECT TARGET SPEC.md`
- the checked-in Python implementation, with tests treated only as secondary corroboration

Use it with the following precedence:

- For what the repository does today, actual code paths and current wiring win.
- For the intended end-state architecture, `PROJECT TARGET SPEC.md` wins.
- `PROJECT PAPER.md` remains useful for target behavior where it does not conflict with the target spec.
- Literal contradictions between the paper and the target spec are handled in `PAPER_SPEC_RECONCILIATION.md`.

Status tags mean:

- `[Implemented]` exists in code and is materially wired into current execution. Tests may raise confidence, but they do not by themselves upgrade a status.
- `[Partial]` exists in meaningful form, but is materially thinner, narrower, or less durable than the target architecture.
- `[Missing]` does not exist in a meaningful operational sense, even if there is naming, scaffolding, or a nearby placeholder.

## Executive Summary

- `[Implemented]` The repository is a real bounded runtime-search workbench: baseline runtime templating, runtime loading, profile-aware runtime identity, benchmark execution, staged evaluation, objective-conditioned archive insertion, mutation, crossover, predictors, Docker-backed evaluation, and CLI commands all exist in the current code path.
- `[Partial]` The repository is still centered on a small synthetic benchmark universe with pre-structured operations. `build-runtime` is a goal-conditioned search path over cloned demo pressure, not a full goal-to-runtime factory with frozen planning artifacts and a deploy-ready solve product.
- `[Missing]` The largest target-spec gaps are the build-time artifact pipeline, the user-request solve path for exported runtimes, durable runtime/deployment packaging, first-class checkpoint-resume, durable promoted-tool assets, realistic benchmark pressure, and predictor-driven task-time decisions.

## Notable Status Changes Since The Previous Version

- `[Implemented]` Runtime ABI checking is now real. `runtime_loader.py` validates `runtime_manifest.json` against `RUNTIME_ABI_VERSION`.
- `[Partial]` Export and provenance bundles are now real artifacts. `build-runtime` writes `runtime_export_bundle.json` and `runtime_provenance_bundle.json`, but there is still no deployment contract, signed provenance, or sealed asset packaging.
- `[Partial]` Benchmark extensibility is no longer absent. `benchmarks.py` supports JSON suite loading plus registered or module-based suite plugins, but there is still no rich benchmark adapter ecosystem.
- `[Partial]` Provider infrastructure is materially broader than before. Replay, retry, failover, payload serialization, environment isolation, basic health checks, and audit trails exist, but provider behavior is still text-generation-centric and lightweight.
- `[Implemented]` Docker evaluation is not just a stub. The evaluator can batch ordered task runs through `container_runtime.py`, forward provider configuration, and rehydrate run results.

## Product Surface And Build Artifact Pipeline

### Implemented

- `[Implemented]` The CLI already exposes `init-runtime`, `solve`, `eval`, `evolve`, and `build-runtime`.
- `[Implemented]` `build-runtime` creates a seed runtime, runs bounded evolution, selects a leader by goal score then validation, exports a runtime directory, and writes a machine-readable build summary.
- `[Implemented]` Benchmark-mode solving is real: `agintor solve <runtime_dir> <task_id> --suite ...` loads a runtime and executes it against a benchmark task.

### Partial

- `[Partial]` `build-runtime` is a goal-conditioned wrapper around the demo suite. It appends cloned train tasks with goal metadata and prompt emphasis, but it does not create the full frozen planning stack required by the target spec.
- `[Partial]` The build workspace is only partly inspectable. It contains the seed runtime, evolution outputs, and build summary, but it does not persist the full chain of normalized planning artifacts the target spec requires.
- `[Partial]` CLI output is structured JSON, but it reports a thinner build story than the target spec expects and omits several planned artifact paths.

### Missing

- `[Missing]` There is no `GoalSpec`, `SuccessCriteriaBundle`, `BenchmarkPlan`, `VerifierBundle`, `RuntimePlan`, or `DeploymentContract` artifact pipeline.
- `[Missing]` There is no user-request solve path for exported runtimes. `agintor solve` currently requires a benchmark task ID rather than accepting a raw solve prompt or request file.
- `[Missing]` There is no target-spec workspace layout with frozen `goal/`, `planning/`, and `export/` artifact families that later stages consume instead of reparsing raw intent.

## Runtime Artifact Contract And Export Packaging

### Implemented

- `[Implemented]` A runtime artifact is a directory with `runtime_manifest.json`, `runtime_profile.json`, and four mutable policy files.
- `[Implemented]` Runtime identity includes the effective runtime profile. `runtime_loader.py` hashes mutable files plus immutable manifest inputs.
- `[Implemented]` `build-runtime` writes `runtime_export_bundle.json` and `runtime_provenance_bundle.json`, including runtime hash, code hash, ABI, provider identity, file digests, and an attestation hash.

### Partial

- `[Partial]` The runtime is still a loose Python directory loaded by an installed Agintor host, not a sealed packaged runtime with a stronger compatibility or deployment boundary.
- `[Partial]` ABI enforcement is currently a string-equality handshake. There is no richer compatibility matrix, migration story, or versioned host capability negotiation.
- `[Partial]` The provenance bundle is self-generated and unsigned. It is useful for traceability, but not a strong attestation or reproducible-build story.
- `[Partial]` The runtime profile still mixes factory-side and runtime-side settings in one physical JSON document, even though the target spec wants a clearer logical split.

### Missing

- `[Missing]` There is no `deployment_contract.json`.
- `[Missing]` There is no packaged durable asset layer for promoted tools, memory snapshots, benchmark adapters, or environment fingerprints.
- `[Missing]` There is no signed provenance, reproducible export manifest, artifact registry integration, or forward/backward migration contract.

## Fixed Runtime Host And Immutable Shell

### Implemented

- `[Implemented]` `FixedShell` already owns the canonical agent pool, short-term graph, long-term graph, message board, open-handle table, predictors, safety guard, sandbox manager, tool registry, tool executor, and trace writing.
- `[Implemented]` Clone-on-run is enforced. `AgentPool.assert_clone()` hard-invalidates direct execution of canonical stored agents.
- `[Implemented]` Task resets enforce long-term memory boundaries. Non-transfer tasks clear long-term memory; transfer-scored episodes preserve it within the episode scope only.
- `[Implemented]` Open-handle integrity and short-term raw-output reachability are hard invariants enforced by the shell and graph classes.

### Partial

- `[Partial]` The shell is process-local and in-memory. There is no durable event store, persisted runtime state store, or replay service.
- `[Partial]` Worker isolation is achieved by deep-copying shell internals in `runner.py`, not by explicit stable snapshot/restore APIs for shell subsystems.
- `[Partial]` The message board exists and is preserved across worker execution, but it is only lightly exercised as a true coordination channel.

### Missing

- `[Missing]` There is no first-class checkpoint/resume manager that can restore open handles, board state, unresolved queues, and suspended branches after process death.
- `[Missing]` There is no runtime-host adapter from a user-facing `SolveRequest` into an internal bounded task envelope.
- `[Missing]` There is no durable replay database, observability UI, or long-running orchestration substrate.

## Mutable Runtime Policy Surface

### Implemented

- `[Implemented]` The mutable surface is still the four policy files named in the runtime manifest.
- `[Implemented]` Stage 0 patch validation enforces SEARCH/REPLACE format, block and line limits, and mutation boundaries inside contracted mutable methods.
- `[Implemented]` Whole-method AST crossover exists and operates per interface without rewriting immutable shell code.

### Partial

- `[Partial]` The policies are still mostly hand-authored heuristics driven by lexical overlap, counters, and profile weights rather than predictor-driven runtime programs.
- `[Partial]` The runtime still carries legacy control-surface methods `score_interface_scope` and `update_scope_credit`, even though the target spec moves that responsibility back to the factory plane.
- `[Partial]` The mutable search surface is still ordinary Python methods with local patching pressure, not a broader helper DSL or richer internal policy library.

### Missing

- `[Missing]` The search loop does not co-evolve the runtime profile, benchmark adapters, or export-time assets.
- `[Missing]` There is no dedicated mutation pressure for simplification, cleanup, or refactoring.
- `[Missing]` There is no deep predictor routing through topology, memory, tooling, and control at task time.

## Goal Interpretation, Success Criteria, And Runtime Planning

### Implemented

- `[Implemented]` The builder has a real, if shallow, goal-conditioning path. `goal_rubric.py` extracts keywords, phrases, and target families, and `runtime_builder.py` uses that to shape benchmark pressure.

### Partial

- `[Partial]` Goal interpretation is keyword-heuristic and family-heuristic only. It does not yet produce stable structured artifacts with explicit assumptions, deployment intent, or measurable success criteria.
- `[Partial]` The build path selects and clones tasks from the demo suite based on heuristic family mapping, but it does not freeze a true runtime plan before evolution.

### Missing

- `[Missing]` There is no `GoalSpec`.
- `[Missing]` There is no success-criteria extraction artifact or weighting model.
- `[Missing]` There is no `RuntimePlan`, `FactoryProfile`, or `DeploymentContract` artifact.
- `[Missing]` There is no bounded runtime-factory planning stage that cleanly separates factory-only settings from runtime-only execution settings.

## Benchmarks And Verifier System

### Implemented

- `[Implemented]` The benchmark model supports train, validation, test, and proxy partitions; proxy scope tags; context items; transfer-scored episodes; and benchmark-task loading from JSON.
- `[Implemented]` The verifier layer supports exact JSON, numeric-tolerant JSON, exact string, exact number, trace-event presence, and trace-event-count checks, plus the `local`, `subtree`, `repo`, and `benchmark` checker ladder.
- `[Implemented]` The suite loader supports registered plugins and module-based plugin factories.

### Partial

- `[Partial]` The shipped suite is still tiny, synthetic, and heavily structured around predeclared operations.
- `[Partial]` Goal-conditioned benchmark pressure is still just cloned demo tasks with prompt emphasis and metadata, not true bounded task synthesis plus verifier freeze.
- `[Partial]` The verifier stack is still a compact local grader family rather than a richer bundle of domain-specific artifact-shape, repo, browser, or service graders.

### Missing

- `[Missing]` There is no explicit `BenchmarkPlan` or `VerifierBundle` written to disk and then consumed by later stages.
- `[Missing]` There is no broad benchmark adapter ecosystem for repo editing, browsers, services, multimodal tasks, or long-horizon workflows.
- `[Missing]` There is no serious verifier-generation or verifier-adaptation stage beyond the hard-coded task verifier types.

## Agent Topology And Task-Time Orchestration

### Implemented

- `[Implemented]` The runtime supports single, vertical, and horizontal solve modes.
- `[Implemented]` Child specs, checkpoint summaries, deterministic horizontal merge, isolated worker execution, and controlled failure on unmet verification are real execution behaviors.
- `[Implemented]` Merge order is deterministic and benchmark-visible in the current runner and topology policy.

### Partial

- `[Partial]` Horizontal workers are still logically parallel only. They execute sequentially in-process.
- `[Partial]` Checkpoints are summary objects with open-handle and artifact references, but there is no restart-from-checkpoint execution path.
- `[Partial]` Task execution still assumes a benchmark task with structured operations rather than a runtime-generated plan over raw goals.

### Missing

- `[Missing]` There is no concurrent scheduler, cancellation/preemption system, or branch-level budget allocator.
- `[Missing]` There is no durable resume workflow for suspended branches or long-lived async work.
- `[Missing]` There is no user-request mode adapter that turns raw prompts into bounded runtime work.

## Short-Term Memory

### Implemented

- `[Implemented]` Short-term memory is an append-only graph with the required node and edge vocabularies.
- `[Implemented]` Summary replacement preserves backlinks and hidden-node reachability, and violations hard-invalidate the run.
- `[Implemented]` Checkpoint publication re-emits summary, artifact, and handle nodes into the short-term graph.

### Partial

- `[Partial]` The graph is still a lightweight in-memory execution record rather than a rich queryable provenance system.
- `[Partial]` The runtime uses only a subset of the graph semantics the paper/spec envision, mostly around evidence, artifacts, and checkpoint summaries.

### Missing

- `[Missing]` There is no persisted provenance store, replay explorer, query API, or trace-diff tooling.
- `[Missing]` There is no robust reconstruction path from graph state back into live runtime state after process loss.

## Long-Term Memory

### Implemented

- `[Implemented]` Long-term memory already supports typed nodes for `Symbol`, `File`, `Query`, `Answer`, `ToolFailure`, `FixPattern`, `TaskNote`, `Procedure`, `EnvironmentFingerprint`, and `ArtifactSignature`.
- `[Implemented]` Retrieval correctly prioritizes exact symbol and exact path matches ahead of lexical and embedding-style similarity.
- `[Implemented]` The memory policy supports `merge`, `refine`, `new`, and `tombstone` write behaviors in code.

### Partial

- `[Partial]` The long-term store is still a flat node map without explicit edges, contradiction tracking, or durable persistence.
- `[Partial]` Embeddings are cheap lexical hashes and not a serious semantic retrieval substrate.
- `[Partial]` Promotion heuristics are still simple scalar heuristics over novelty, reuse, verifier support, and duplication risk.

### Missing

- `[Missing]` There is no persisted cross-run knowledge base.
- `[Missing]` There is no contradiction-resolution system, retrieval diagnostics surface, or versioned memory graph.
- `[Missing]` `EnvironmentFingerprint` exists only as schema vocabulary; the runtime does not meaningfully extract or use it from real execution.

## Tooling System And Sandbox

### Implemented

- `[Implemented]` Tool usage follows category-first discovery, category slicing, candidate collection, tool ranking, build-vs-reuse gating, optional synthesis, validation, dispatch, async handle tracking, and promotion decisions.
- `[Implemented]` Generated tool validation is substantial for the current MVP: syntax, signature checks, import resolution, permission checks, timeout runs, smoke tests, and deterministic replay all exist.
- `[Implemented]` Sandbox reuse is content-addressed by a hash over tool source and execution inputs.
- `[Implemented]` Async tool launch, wait, handle tracking, and failure propagation are real runtime features.

### Partial

- `[Partial]` Generated tools are still mostly expression-driven Python tools; this is not yet a broad multi-runtime tool-construction ecosystem.
- `[Partial]` Tool promotion uses pass-rate, reuse-count, safety, and determinism thresholds, but promotion only affects the current registry; it does not create a durable reusable asset that survives export as a first-class package.
- `[Partial]` Async exists, but the runner usually waits on handles immediately, so overlap is limited.
- `[Partial]` Safety and sandboxing are still Python-level AST and subprocess controls, not hardened OS-level isolation.

### Missing

- `[Missing]` There is no durable promoted-tool registry with provenance, rollback, sharing, and export integration.
- `[Missing]` There is no multi-language, dependency-managed tool builder or stateful external-service tool layer.
- `[Missing]` There is no hardened sandbox stack with network controls, resource quotas, filesystem policy enforcement, syscall/process controls, or audit logs.
- `[Missing]` There is no background-job lifecycle manager for retries, cancellation, orphan cleanup, or crash recovery.

## Control System And Predictors

### Implemented

- `[Implemented]` Solve-time control already covers model assignment, checker requests, stop policy, budget accounting, and one-step escalation after repeated negative progress.
- `[Implemented]` The predictor layer exists with bootstrapped probability and positive-scalar models, retraining thresholds, freezing during evaluation, and observation extraction from traces.

### Partial

- `[Partial]` Control decisions are still mostly heuristic; predictors are trained, but the runtime only lightly consumes them today.
- `[Partial]` Feature vectors are trace/count summaries rather than rich runtime-state features.
- `[Partial]` The current control surface still mixes in legacy factory-adjacent methods, which is a target-spec ownership mismatch even though the factory already owns real scheduler state updates.

### Missing

- `[Missing]` There is no deep predictor-driven task-time routing using conservative and optimistic utilities across topology, memory, tooling, and stopping.
- `[Missing]` There is no calibration monitoring surface, uncertainty diagnostics UI, or systematic predictor observability.
- `[Missing]` There is no branch-level action-value accounting or rollback planning.

## Provider Layer

### Implemented

- `[Implemented]` The repository now has local deterministic, OpenAI, MiniMax, replay, retry, and failover providers.
- `[Implemented]` Provider payload serialization and rehydration work across Docker boundaries, including mounted key files and replay files.
- `[Implemented]` Runtime execution scrubs unrelated provider environment variables before task execution.
- `[Implemented]` Basic provider audit and health surfaces exist through `RetryProvider.audit_trail()` and `RetryProvider.health_check()`.

### Partial

- `[Partial]` The provider abstraction is still centered on text generation requests and simple response payloads.
- `[Partial]` Retry and failover logic are simple token-based heuristics over exception text, not robust provider-specific policies.
- `[Partial]` Audit, health, and replay are useful but lightweight and local to the wrapper classes.

### Missing

- `[Missing]` There is no batching API, streaming API, tool-calling abstraction, or structured hosted-response contract in the provider layer.
- `[Missing]` There is no central provider observability or health monitoring system beyond local wrappers.
- `[Missing]` There is no rich offline replay/capture framework beyond flat recorded response rows.

## Evaluation, Scoring, And Validation

### Implemented

- `[Implemented]` The evaluator supports staged gates, common-random-number comparisons, reference-scale estimation, family/global objective scoring, shrinkage robustness, CVaR tie-break statistics, full-train minibatch early rejection, and validation evaluation.
- `[Implemented]` Train, validation, and test partitions are materially isolated in the evaluator and mutation-prompt construction code, and validation/test traces are excluded from mutation prompts.

### Partial

- `[Partial]` Stage 0 still stops at patch-format, boundary, and parse/load integrity. It does not run formatter, linter, or broader unit-test gates as described in the paper.
- `[Partial]` Validation exists as an evaluator call and export tie-break signal, but not as a richer tracked leaderboard/report surface with frozen planning artifacts.
- `[Partial]` Docker evaluation isolates whole runtime executions, not per-tool or per-branch sandboxes.

### Missing

- `[Missing]` There is no experiment database, contamination-controlled held-out program, or distributed evaluation harness.
- `[Missing]` There is no serious grader family for repo, browser, service, multimodal, or long-horizon tasks.
- `[Missing]` There is no persisted validation-history or stage-failure reporting contract matching the target-spec workspace plan.

## Evolution Loop, Archive, And Search State

### Implemented

- `[Implemented]` The evolution loop already has objective sampling, scope scheduling, heuristic or provider patch mutation, AST crossover, staged evaluation, archive insertion, predictor updates, and counterfactual singleton/pair credit updates.
- `[Implemented]` The archive tracks objective, behavior descriptor, scope tag, interface-difference mask, and complexity bucket.
- `[Implemented]` Stage pass-rate counters can tighten thresholds when too many children pass early gates.

### Partial

- `[Partial]` Objective selection is still uniform random rather than adaptive to archive need or uncertainty.
- `[Partial]` Search state is mostly in-memory plus `evolution_history.json`; it is not a robust resumable long-running system.
- `[Partial]` The mutators are still local patch operators over the existing heuristics, not broader self-programming transformations.

### Missing

- `[Missing]` There is no operator portfolio adaptation, search checkpoint/restart, distributed island execution, or lineage browser.
- `[Missing]` There is no dedicated simplification/refactoring operator.
- `[Missing]` There is no champion promotion/rollback workflow beyond export-time leader copying.

## Docker And Runtime Isolation

### Implemented

- `[Implemented]` The evaluator can execute runtimes locally or inside Docker, and Docker execution preserves ordered task batches plus provider/config payload forwarding.

### Partial

- `[Partial]` Docker isolation is still repo-wide and runtime-wide rather than per tool, per branch, or per capability boundary.
- `[Partial]` Container execution is useful as a packaging and environment boundary, but it is still thin compared with the target-spec sandbox ambitions.

### Missing

- `[Missing]` There is no network restriction, capability dropping, cgroup quota enforcement, or attested container environment story.
- `[Missing]` There is no per-tool sandbox backend with stronger process and filesystem controls.

## Current State Of Completion

### Implemented

- `[Implemented]` The repository already proves the bounded-runtime-search architecture end to end for the current MVP problem class.
- `[Implemented]` The current codebase already enforces many architecture edges directly in runtime and evaluator logic: mutation boundaries, graph invariants, async handles, provider forwarding, runtime identity, archive behavior, crossover, and builder export logic.

### Partial

- `[Partial]` The repository is still an MVP runtime-search workbench for small synthetic structured tasks rather than a full runtime factory product.
- `[Partial]` The strongest current evidence is around architecture enforcement and bounded search mechanics, not around open-ended multi-agent capability.

### Missing

- `[Missing]` The project does not yet demonstrate robust domain-specialized runtime improvement on serious held-out suites or a deployable user-facing MAS workflow after export.

## Highest-Leverage Remaining Work

- Add the build-time artifact chain first: `GoalSpec`, success criteria, benchmark plan, verifier bundle, runtime plan, and deployment contract.
- Add the exported-runtime user-request solve path next. Without it, the product is still benchmark-only after export.
- Move remaining factory-only control concepts out of the runtime control surface so the implementation matches the target-spec ownership model.
- Make checkpoint/resume first-class instead of summary-only.
- Turn promoted/generated tools into durable exported assets rather than task-local registry entries.
- Replace cloned-demo goal pressure with stronger bounded benchmark synthesis or richer benchmark adapters.
- Route actual solve-time policy decisions through predictors instead of leaving predictors mostly on the mutation-analysis side.
