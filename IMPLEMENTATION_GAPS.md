# Implementation Gap Crosswalk (AGENTS vs Target Spec)

This inventory extracts every `[Partial]` and `[Missing]` status item from `AGENTS.md` and maps each domain to relevant target sections in `PROJECT TARGET SPEC.md`.

- Total gaps identified: **104** (51 partial, 53 missing).
- Method: parse status-tagged bullets in `AGENTS.md`; use `PROJECT TARGET SPEC.md` sections as target-state anchors.

## What The Repository Already Is
### Partial
- What exists is a coherent MVP runtime-search workbench, not a production MAS-factory.

## Product Architecture In One Flow
Target spec anchors: §1. Scope, Design Goals, and Core Notation (line 13), §3. Runtime State, Evaluation Unit, and Hard Invalidation (line 113).
### Partial
- The task model currently arrives pre-structured as benchmark operations, so the runtime is optimizing execution strategy around structured tasks rather than inventing arbitrary task graphs from raw goals.

## Runtime Artifact Model
Target spec anchors: §2. Fixed Shell, Mutable Genotype, and Mandatory Schemas (line 45), §13. Deterministic Implementation Notes (line 987).
### Partial
- The runtime artifact is still a loose local Python directory, not a sealed packaged runtime with a compatibility contract.
- The runtime profile strongly shapes execution, but it is not currently part of the mutable search surface.
### Missing
- There is no explicit runtime ABI/version handshake between exported runtimes and the shell/runtime API.
- There is no signed provenance bundle, no reproducible export manifest, and no artifact attestation story.
- There is no packaged durable asset layer for promoted tools, memory snapshots, benchmark adapters, or environment metadata.

## Mutable Genotype
Target spec anchors: §2. Fixed Shell, Mutable Genotype, and Mandatory Schemas (line 45), §11. Mutation Contract, Prompt, and Curriculum (line 885).
### Partial
- These policies are still handwritten heuristic controllers driven mostly by profile weights, lexical overlap, and simple counters.
- Evolution searches only the four policy files and does not co-evolve the runtime profile, benchmark adapters, or shell internals.
- The current search space is still mostly line-local heuristic adjustment, not broader self-programming logic.
### Missing
- There is no richer mutable helper-surface library or internal DSL for policies beyond ordinary Python methods.
- There is no mutation pressure for cleanup, simplification, code compression, or policy refactoring.
- There is no learned or co-evolved family of runtime programs approaching the spec's intended decision sophistication.

## Fixed Shell
Target spec anchors: §2. Fixed Shell, Mutable Genotype, and Mandatory Schemas (line 45), §3. Runtime State, Evaluation Unit, and Hard Invalidation (line 113).
### Partial
- Shell immutability is enforced by the manifest and evaluator mutation boundary, not by a separately packaged binary/runtime boundary.
- The shell is process-local and in-memory; it is not yet a durable orchestration substrate with persistence, resume services, or an event store.
### Missing
- There is no first-class checkpoint/resume manager that can restore open handles, board state, and suspended branches.
- There is no persistent shell state store, replay database, or observability surface for long-running runtime evolution.
- There is no hardened sandbox/security manager beyond Python-level validation and the thin Docker runner.

## Provider Boundary
Target spec anchors: §2. Fixed Shell, Mutable Genotype, and Mandatory Schemas (line 45), §13. Deterministic Implementation Notes (line 987).
### Partial
- The provider abstraction is still text-generation-centric; it does not expose tool calling, batching, streaming control, structured retries, or failover.
### Missing
- There is no provider health-check layer, rate-limit handling strategy, failover routing, audit trail, or offline replay stub for hosted generations.

## Benchmarks And Task Model
Target spec anchors: §4. Evaluation Setting, Objectives, and Statistical Protocol (line 160), §12. Staged Evaluation and Compute Control (line 941).
### Partial
- The benchmark suite is still tiny, synthetic, hard-coded in Python, and heavily structured around predeclared operations.
- Many tasks already tell the runtime what operations exist, so the benchmark pressure is still much narrower than real-world open-ended MAS work.
### Missing
- There is no benchmark DSL, plugin adapter system, domain-specific task loader layer, or realistic external-task integration.
- There are no serious repo-editing, browser, external-service, multimodal, or long-horizon benchmark families yet.

## Agent System
Target spec anchors: §7. Evolution of Self-Generating Topology (line 495), §3. Runtime State, Evaluation Unit, and Hard Invalidation (line 113).
### Partial
- Horizontal workers are logically parallel only. They currently execute sequentially in-process, not concurrently.
- The message board is still mostly a contract placeholder. Workers do not yet use it as a meaningful coordination channel.
- Ephemeral agent creation is still shallow `AgentTemplate` instantiation, not durable runtime-owned agent programming.
### Missing
- There is no true concurrent scheduler, cancellation/preemption system, branch-level budget manager, or durable inter-agent protocol layer.
- There are no evolving agent classes, no persistent specialization state, and no role-specific lifecycle management beyond template cloning.

## Short-Term Memory
Target spec anchors: §8. Evolution of Hierarchical Memory (line 593).
### Partial
- The graph is still a lightweight in-memory execution record, not a rich queryable provenance system.
- The full spec vocabulary exists, but the runtime currently exercises only a subset of the deeper graph semantics.
### Missing
- There is no persisted short-term provenance store, no replay explorer, no trace query API, and no diff/debug tooling over compaction behavior.
- There is no robust resume reconstruction path from graph state back into live execution state.

## Long-Term Memory
Target spec anchors: §8. Evolution of Hierarchical Memory (line 593).
### Partial
- The long-term store behaves more like a typed retrieval cache than like the richer graph-memory system described by the spec.
- Embeddings are cheap lexical hash embeddings, not serious semantic retrieval infrastructure.
- Dedup/upsert supports `merge`, `refine`, and `tombstone` paths in principle, but current policy behavior mostly exercises `merge` or `new`.
### Missing
- There is no persisted cross-run knowledge base, no explicit memory-edge graph, no contradiction-resolution system, and no retrieval diagnostics surface.
- There is no strong notion of reusable abstractions, procedures-with-provenance, or environment fingerprints extracted from real execution.

## Tooling System
Target spec anchors: §9. Evolution of Dynamic Tooling (line 703).
### Partial
- Tool synthesis is still mostly expression-driven Python code generation, not a broader tool-construction ecosystem.
- Async execution exists, but the runner usually awaits the handle immediately, so the system does not yet exploit meaningful overlap.
- Tool promotion thresholds exist, but promotion does not create a durable reusable registry that survives task resets or export.
- Generated tools are still effectively task-local, so distinct-task reuse is measured but not operationalized into durable evolution.
### Missing
- There is no serious dependency-managed multi-runtime tool builder beyond Python source materialization.
- There is no reusable promoted-tool registry with versioning, rollback, provenance, export packaging, or sharing across runtimes.
- There is no large permissioned tool ecosystem, no external service tool layer, and no stateful durable tool substrate.
- There is no production-grade sandbox policy enforcement with resource quotas, secret isolation, network allowlists, syscall/process controls, or audit logs.
- There is no background-job lifecycle management for retries, cancellation, orphan cleanup, or crash recovery.

## Control System
Target spec anchors: §10. Budget, Verification, and Stopping Control (line 812), §5. Predictor Families, Uncertainty, and Online Calibration (line 280).
### Partial
- This is still a hand-authored VOI-style controller, not the predictor-backed control surface from the spec.
- Model assignment currently matters mostly for bookkeeping and auxiliary prompt calls. The runtime still lacks a genuine general-purpose model-driven reasoning loop.
- Confidence, uncertainty, and terminal utility are still simplified approximations rather than calibrated runtime estimates.
### Missing
- There is no predictor-driven runtime control path using conservative and optimistic bounds during task-time decisions.
- There is no branch-level budget allocation, rollback planning, or explicit action-value accounting across concurrent branches.

## Inner-Loop Task Execution
Target spec anchors: §3. Runtime State, Evaluation Unit, and Hard Invalidation (line 113), §7. Evolution of Self-Generating Topology (line 495).
### Partial
- The runtime is still operating on benchmark-specified operation graphs rather than synthesizing rich plans from unstructured goals.
- Snapshot/restore for isolated branches is still ad hoc and mutates private internals of the shell subsystems.
- Checkpoints exist as objects and summaries, but there is no actual restart-from-checkpoint execution path.
### Missing
- There is no true suspend/resume workflow, no durable continuation of long-lived async branches, and no generalized planner/model execution loop.
- There is no robust recovery taxonomy separating invalidation, verifier failure, provider failure, transient tool failure, and resumable branch failure.

## Evaluation And Scoring
Target spec anchors: §4. Evaluation Setting, Objectives, and Statistical Protocol (line 160), §12. Staged Evaluation and Compute Control (line 941).
### Partial
- Stage 0 is stricter than a simple parse check, but it still does not run formatter/linter/unit-test gates beyond AST and boundary validation.
- The staged evaluator is structurally aligned with the spec, but the benchmark universe is still a tiny demo suite.
- The verifier stack is still synthetic and exact-match or trace-proxy oriented, not the broader family of benchmark-specific graders the full system will need.
- Docker evaluation isolates whole runtime units, not per-tool execution or benchmark-specific sandbox policies.
### Missing
- There is no serious benchmark adapter ecosystem, no contamination-governed held-out program, no distributed evaluation harness, and no experiment database.
- There is no production-grade grader family for repos, browsers, services, or long-horizon workflows.

## Evolution Loop
Target spec anchors: §6. Archive Design, Diversity Descriptors, and Outer-Loop Controller (line 356), §11. Mutation Contract, Prompt, and Curriculum (line 885).
### Partial
- The heuristic mutator is still a narrow hard-coded search over a few local replacements.
- The provider-backed mutator is still constrained to small local patches inside four files.
- Objective selection is uniform random rather than adaptive to archive state, uncertainty, or diminishing returns.
- Evolution state is mostly in-memory plus `evolution_history.json`; resumable long-running search is underbuilt.
### Missing
- There is no operator portfolio adaptation, no search checkpoint/restart system, no multi-parent crossover, and no distributed island execution.
- There is no dedicated simplification/refactoring operator that can reduce heuristic cruft while preserving behavior.

## Predictors
Target spec anchors: §5. Predictor Families, Uncertainty, and Online Calibration (line 280).
### Partial
- The predictors are not yet deeply consumed by the runtime policies themselves.
- Feature vectors are still simple trace/count summaries rather than rich runtime-state features.
- The spec envisions conservative and optimistic utilities throughout topology, tooling, memory, and control; current code trains models but barely routes decisions through them.
### Missing
- There is no meaningful calibration/monitoring loop, no uncertainty diagnostics surface, and no deep predictor integration into task-time policy decisions.

## Goal-Conditioned Runtime Builder
Target spec anchors: §14. Minimal Reconstruction Sequence (line 1000).
### Partial
- This is an MVP approximation of runtime synthesis from intent, not a true goal-to-runtime compiler.
- The builder currently clones demo benchmark tasks with goal metadata rather than generating goal-native evaluation pressure.
- Goal-family inference is keyword heuristic and shallow.
### Missing
- There is no goal-to-benchmark compiler, no domain-specific verifier generation, no acceptance-test synthesis, and no runtime deployment packaging.

## Docker And Runtime Isolation
Target spec anchors: §3. Runtime State, Evaluation Unit, and Hard Invalidation (line 113), §13. Deterministic Implementation Notes (line 987).
### Partial
- Docker isolation is useful but still thin compared with the spec's stronger sandbox and environment-control ambitions.
- The container image is repo-wide and evaluation-wide, not a hardened per-tool or per-branch sandbox.
### Missing
- There is no network restriction, capability dropping, cgroup/timeout enforcement at the container level, or attested runtime environment story.

## Current State Of Completion
Target spec anchors: §17. Closing Statement (line 1047).
### Partial
- The present code is still an MVP shell-plus-baseline-runtime for small synthetic structured tasks, not yet a benchmark-grade runtime search platform.
- The architecture is worth building on, but the current behaviors prove the scaffolding more than they prove deep agent capability.
### Missing
- The project does not yet demonstrate robust domain-specialized runtimes that clearly and repeatedly outperform the seed runtime on large held-out suites.

## Major Gaps That Still Matter
Target spec anchors: §17. Closing Statement (line 1047).
### Partial
- Topology, memory, tooling, and control are separated correctly, but they are still mostly heuristic policies rather than genuinely co-evolved, predictor-driven runtime programs.
- The runtime does not yet do rich model-driven planning over open-ended tasks; it mostly executes benchmark-specified operation graphs.
- Checkpoint/resume, durable tool promotion, richer long-term memory, and goal-conditioned building all exist only in thin MVP form.
### Missing
- The system still lacks realistic benchmark pressure, production-grade durability/isolation, and predictor-driven task-time decisions.
- The project still lacks evidence of robust domain-specialized runtime improvement on serious held-out suites.

## Additional Missing Systems That Separate This Repo From A Production MAS-Factory
Target spec anchors: §17. Closing Statement (line 1047).
### Missing
- A runtime ABI/versioning story for exported runtimes, shell compatibility, and forward/backward migration.
- A durable promoted-tool registry with provenance, versioning, rollback, sharing, and export integration.
- A real multi-runtime tool builder supporting package installation, multiple languages, stateful services, and reproducible build environments.
- A hardened sandbox stack with process isolation, filesystem policies, secret handling, network controls, quotas, and audit logs.
- A durable checkpoint/resume system that can restore board state, open handles, unresolved goals, branch queues, and verifier state after process death.
- Real concurrent execution for workers, background tools, and long-lived branches instead of mostly sequential in-process simulation.
- A richer long-term memory graph with explicit edges, contradiction handling, provenance reasoning, versioning, and retrieval debugging.
- Provenance-grade replay tooling for short-term traces, summary replacement, and execution diffs across candidate runtimes.
- A benchmark/plugin ecosystem for repo tasks, browser tasks, service orchestration, multimodal tasks, and long-horizon episodes.
- Strong verifier families and benchmark-specific graders that make archive scores meaningful outside the synthetic demo suite.
- Predictor-driven task-time decision making with calibrated uncertainty and conservative/optimistic action routing.
- Distributed search execution, resumable archive state, experiment tracking, lineage browsing, and champion promotion/rollback workflows.
- Runtime export packaging that includes deployment metadata, environment fingerprints, provider contracts, and reusable assets.
- Goal-native benchmark synthesis and domain-conditioned acceptance criteria rather than cloned benchmark tasks with prompt emphasis.
- Observability surfaces for runtime traces, tool lifecycle, memory promotion, predictor drift, and archive evolution.
- A deployment story for produced runtimes: registry, artifact promotion, rollback, compatibility validation, and runtime-health monitoring.

