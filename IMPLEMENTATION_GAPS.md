# Implementation Gaps

This document is the current-state crosswalk between:

- `PROJECT PAPER.md`
- `PROJECT TARGET SPEC.md`
- the checked-in Python implementation, with tests treated only as secondary corroboration

Use it with the following precedence:

- For what the repository does today, actual code paths and current wiring win.
- For the intended end-state architecture, `PROJECT TARGET SPEC.md` wins.
- `PROJECT PAPER.md` remains useful for target behavior where it does not conflict with the target spec.
- Literal contradictions between the paper and the target spec are handled inline in the relevant sections below.

Status tags mean:

- `[Implemented]` exists in code and is materially wired into current execution. Tests may raise confidence, but they do not by themselves upgrade a status.
- `[Partial]` exists in meaningful form, but is materially thinner, narrower, or less durable than the target architecture.
- `[Missing]` does not exist in a meaningful operational sense, even if there is naming, scaffolding, or a nearby placeholder.

## Executive Summary

- `[Implemented]` The repository is a real bounded runtime-search workbench: baseline runtime templating, runtime loading, profile-aware runtime identity, benchmark execution, staged evaluation, objective-conditioned archive insertion, mutation, crossover, predictors, Docker-backed evaluation, and CLI commands all exist in the current code path.
- `[Partial]` The repository is still centered on a small synthetic benchmark universe with pre-structured operations. `build-runtime` is a goal-conditioned search path over cloned demo pressure with frozen planning artifacts and a bounded prompt-mode solve path, not a full goal-to-runtime factory or a broad deploy-ready solve product.
- `[Missing]` The largest target-spec gaps are a trustworthy goal-to-objective compiler, contradiction-driven replanning, durable runtime/deployment packaging beyond the current artifact set, first-class checkpoint-resume, durable promoted-tool assets, realistic benchmark pressure, and predictor-driven task-time decisions.

## Product Surface And Build Artifact Pipeline

### Implemented

- `[Implemented]` The CLI already exposes `init-runtime`, `solve`, `eval`, `evolve`, and `build-runtime`.
- `[Implemented]` `build-runtime` creates a seed runtime, runs bounded evolution, selects a leader by goal score then validation, exports a runtime directory, and writes a machine-readable build summary.
- `[Implemented]` `build-runtime` writes `goal_spec.json`, `success_criteria.json`, `benchmark_plan.json`, `verifier_bundle.json`, `factory_profile.json`, and `runtime_plan.json` into a structured `goal/`, `planning/`, `export/`, and `evolution/` workspace.
- `[Implemented]` Benchmark-mode solving is real: `agintor solve <runtime_dir> <task_id> --suite ...` loads a runtime and executes it against a benchmark task.
- `[Implemented]` Prompt-mode solving is real in bounded form: `agintor solve <runtime_dir> --prompt ...` or `--prompt-file ...` loads a `SolveRequest`, adapts it into a constrained internal task envelope, and returns a `SolveResult`.

### Partial

- `[Partial]` `build-runtime` is a goal-conditioned wrapper around the demo suite. It appends cloned train tasks with goal metadata and prompt emphasis, and it freezes a planning stack, but the planning logic is still heuristic and narrower than the target spec.
- `[Partial]` The builder persists canonical planning artifacts, but the pipeline still passes live Python objects between stages instead of reloading each stage strictly from the serialized artifacts it just wrote.
- `[Partial]` The build workspace is materially inspectable. It contains the seed runtime, frozen planning artifacts, evolution outputs, leaderboard, validation history, stage failures, and export summaries, but it still omits richer long-lived experiment and provenance reporting surfaces.
- `[Partial]` CLI output is structured JSON and includes the main planning and export paths, but it still omits some richer reporting references such as validation-history, stage-failure, and archive-report paths.
- `[Partial]` The user-request solve path is bounded. Prompt input is translated into a constrained task envelope rather than a broader open-ended runtime plan over raw user intent.

## Runtime Artifact Contract And Export Packaging

### Implemented

- `[Implemented]` A runtime artifact is a directory with `runtime_manifest.json`, `runtime_profile.json`, `deployment_contract.json`, and four mutable policy files.
- `[Implemented]` Runtime identity includes the effective runtime profile. `runtime_loader.py` hashes mutable files plus immutable manifest inputs.
- `[Implemented]` `build-runtime` writes `runtime_export_bundle.json` and `runtime_provenance_bundle.json`, including runtime hash, code hash, ABI, provider identity, file digests, and an attestation hash.
- `[Implemented]` `runtime_loader.py` validates `deployment_contract.json` against `RUNTIME_ABI_VERSION`, Python version requirements, and supported backends.

### Partial

- `[Partial]` The runtime is still a loose Python directory loaded by an installed Agintor host, not a sealed packaged runtime with a stronger compatibility or deployment boundary.
- `[Partial]` ABI and deployment-contract enforcement go beyond string equality: the loader validates runtime ABI, Python version requirements, and supported backend claims. There is still no richer compatibility matrix, migration story, or versioned host capability negotiation.
- `[Partial]` The provenance bundle is self-generated and unsigned. It is useful for traceability, but not a strong attestation or reproducible-build story.
- `[Partial]` The runtime profile still mixes factory-side and runtime-side settings in one physical JSON document, even though the build and export flow reconstructs a clearer logical split.
- `[Partial]` Exported runtimes still resolve immutable support modules such as `agintor/runner.py`, `agintor/shell.py`, and `agintor/tool_runtime.py` from the installed host package. There is no bundled runtime kernel or runtime-owned SDK in the export.

### Missing

- `[Missing]` There is no packaged durable asset layer for promoted tools, memory snapshots, benchmark adapters, or environment fingerprints.
- `[Missing]` There is no signed provenance, reproducible export manifest, artifact registry integration, or forward/backward migration contract.

## Fixed Runtime Host And Immutable Shell

### Implemented

- `[Implemented]` `FixedShell` already owns the canonical agent pool, short-term graph, long-term graph, message board, open-handle table, predictors, safety guard, sandbox manager, tool registry, tool executor, and trace writing.
- `[Implemented]` Clone-on-run is enforced. `AgentPool.assert_clone()` hard-invalidates direct execution of canonical stored agents.
- `[Implemented]` Task resets enforce long-term memory boundaries. Non-transfer tasks clear long-term memory; transfer-scored episodes preserve it within the episode scope only.
- `[Implemented]` Open-handle integrity and short-term raw-output reachability are hard invariants enforced by the shell and graph classes.
- `[Implemented]` The runtime host supports a user-facing `SolveRequest` path and adapts it into a bounded internal task envelope.

### Partial

- `[Partial]` The shell is process-local and in-memory. There is no durable event store, persisted runtime state store, or replay service.
- `[Partial]` Worker isolation is achieved by deep-copying shell internals in `runner.py`, not by explicit stable snapshot/restore APIs for shell subsystems.
- `[Partial]` The message board exists and is preserved across worker execution, but it is only lightly exercised as a true coordination channel.

### Missing

- `[Missing]` There is no first-class checkpoint/resume manager that can restore open handles, board state, unresolved queues, and suspended branches after process death.
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

- `[Implemented]` The builder has a real, if shallow, goal-conditioning path. `goal_rubric.py` produces `GoalSpec` and success-criteria artifacts, and `runtime_builder.py` writes `BenchmarkPlan`, `VerifierBundle`, `FactoryProfile`, `RuntimePlan`, and deployment-contract artifacts into the build workspace.
- `[Implemented]` The build path freezes a runtime plan before evolution begins.

### Partial

- `[Partial]` Goal interpretation is keyword-heuristic and family-heuristic only. It does produce structured artifacts with explicit assumptions, deployment intent, and measurable success criteria, but those artifacts remain shallow and template-driven.
- `[Partial]` The build path selects and clones tasks from the demo suite based on heuristic family mapping, and it freezes a runtime plan before evolution, but the plan remains thin and demo-suite-conditioned.
- `[Partial]` The build flow preserves a logical split between factory-only and runtime-only payloads, but the source profile format still mixes both concerns physically.
- `[Partial]` The planning artifacts are persisted to disk, but later build stages still consume live in-memory objects rather than reloading the canonical serialized artifacts between stages.

### Missing

- `[Missing]` There is no contradiction-driven replanning loop that repairs bad upstream interpretations once downstream evidence shows they were wrong.
- `[Missing]` There is no trustworthy goal-to-objective compiler that turns broad natural-language intent into a credible frozen evaluation world.

## Benchmarks And Verifier System

### Implemented

- `[Implemented]` The benchmark model supports train, validation, test, and proxy partitions; proxy scope tags; context items; transfer-scored episodes; and benchmark-task loading from JSON.
- `[Implemented]` The verifier layer supports exact JSON, numeric-tolerant JSON, exact string, exact number, trace-event presence, and trace-event-count checks, plus the `local`, `subtree`, `repo`, and `benchmark` checker ladder.
- `[Implemented]` The suite loader supports registered plugins and module-based plugin factories.
- `[Implemented]` `build-runtime` writes an explicit `BenchmarkPlan` and `VerifierBundle` to disk as part of the build path.

### Partial

- `[Partial]` The shipped suite is still tiny, synthetic, and heavily structured around predeclared operations.
- `[Partial]` Goal-conditioned benchmark pressure is still just cloned demo tasks with prompt emphasis and metadata. Benchmark and verifier artifacts are frozen, but they are frozen around that narrow demo-conditioned pressure rather than around richer typed benchmark planning.
- `[Partial]` The verifier stack is still a compact local grader family rather than a richer bundle of domain-specific artifact-shape, repo, browser, or service graders.

### Missing

- `[Missing]` There is no broad benchmark adapter ecosystem for repo editing, browsers, services, multimodal tasks, or long-horizon workflows.
- `[Missing]` There is no serious verifier-generation or verifier-adaptation stage beyond the hard-coded task verifier types.

## Agent Topology And Task-Time Orchestration

### Implemented

- `[Implemented]` The runtime supports single, vertical, and horizontal solve modes.
- `[Implemented]` Child specs, checkpoint summaries, deterministic horizontal merge, isolated worker execution, and controlled failure on unmet verification are real execution behaviors.
- `[Implemented]` Merge order is deterministic and benchmark-visible in the current runner and topology policy.
- `[Implemented]` The runtime supports bounded user-request mode through prompt-to-task adaptation.

### Partial

- `[Partial]` Horizontal workers are still logically parallel only. They execute sequentially in-process.
- `[Partial]` Checkpoints are summary objects with open-handle and artifact references, but there is no restart-from-checkpoint execution path.
- `[Partial]` Task execution still assumes either a benchmark task with structured operations or a bounded prompt-derived task envelope rather than a richer runtime-generated plan over raw goals.

### Missing

- `[Missing]` There is no concurrent scheduler, cancellation/preemption system, or branch-level budget allocator.
- `[Missing]` There is no durable resume workflow for suspended branches or long-lived async work.

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

- `[Partial]` The provider abstraction is still centered on `ModelRequest` and `ModelResponse` text-generation flows, even though hosted adapters now preserve some metadata such as response IDs, status, usage, latency, and reasoning effort in `ModelResponse.raw`.
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
- `[Implemented]` `evolution.py` persists `validation_history.json` and `stage_failures.json` alongside `evolution_history.json` and `archive_index.json`.

### Partial

- `[Partial]` Stage 0 still stops at patch-format, boundary, and parse/load integrity. It does not run formatter, linter, or broader unit-test gates as described in the paper.
- `[Partial]` Validation, leaderboard, and stage-failure reporting now persist to the workspace, but there is still no richer experiment database, held-out campaign tracking, or contamination-governed reporting surface.
- `[Partial]` Docker evaluation isolates whole runtime executions, not per-tool or per-branch sandboxes.

### Missing

- `[Missing]` There is no experiment database, contamination-controlled held-out program, or distributed evaluation harness.
- `[Missing]` There is no serious grader family for repo, browser, service, multimodal, or long-horizon tasks.

## Evolution Loop, Archive, And Search State

### Implemented

- `[Implemented]` The evolution loop already has objective sampling, scope scheduling, heuristic or provider patch mutation, AST crossover, staged evaluation, archive insertion, predictor updates, and counterfactual singleton/pair credit updates.
- `[Implemented]` The archive tracks objective, behavior descriptor, scope tag, interface-difference mask, and complexity bucket.
- `[Implemented]` Stage pass-rate counters can tighten thresholds when too many children pass early gates.

### Partial

- `[Partial]` Objective selection is still uniform random rather than adaptive to archive need or uncertainty.
- `[Partial]` Search state now persists `evolution_history.json`, `archive_index.json`, `validation_history.json`, and `stage_failures.json`, but the engine still cannot resume from those artifacts as a first-class checkpointed search process.
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
- `[Implemented]` The current codebase already enforces many architecture edges directly in runtime and evaluator logic: mutation boundaries, graph invariants, async handles, provider forwarding, runtime identity, archive behavior, crossover, builder export logic, frozen planning artifacts, deployment contracts, and bounded prompt-mode solve.

### Partial

- `[Partial]` The repository is still an MVP runtime-search workbench for small synthetic structured tasks rather than a full runtime factory product.
- `[Partial]` The strongest current evidence is around architecture enforcement and bounded search mechanics, not around open-ended multi-agent capability.

### Missing

- `[Missing]` The project does not yet demonstrate robust domain-specialized runtime improvement on serious held-out suites or a self-contained exported runtime that can execute without host implementation reach-through.

## Highest-Leverage Remaining Work

- Strengthen the goal-to-objective compiler so the existing `GoalSpec`, success-criteria, benchmark-plan, verifier-bundle, and runtime-plan pipeline becomes domain-richer and less heuristic.
- Add contradiction-driven replanning when downstream evidence shows the frozen early interpretation was wrong.
- Establish a real host/runtime boundary so exported runtimes stop depending on installed host implementation modules for solve-time execution.
- Move remaining factory-only control concepts out of the runtime control surface so the implementation matches the target-spec ownership model.
- Make checkpoint/resume first-class instead of summary-only.
- Turn promoted/generated tools into durable exported assets rather than task-local registry entries.
- Replace cloned-demo goal pressure with stronger bounded benchmark synthesis or richer benchmark adapters.
- Route actual solve-time policy decisions through predictors instead of leaving predictors mostly on the mutation-analysis side.
