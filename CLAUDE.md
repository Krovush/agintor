# Agintor Working Architecture Map

This file is the repository-level working map for coding agents and maintainers.
`PROJECT TARGET SPEC.md` is the target design.
This document is the synthesis of that spec, the current implementation, and the practical gap between them.

Use it with the following rules:
- For current implementation status, code and tests win over this file.
- For intended end-state architecture, the spec wins over the current code.
- Do not upgrade a status tag based on naming, scaffolding, or a placeholder type alone.
- Do not describe the current repository as a production MAS-factory. It is not there yet.

Status tags mean:
- `[Implemented]` exists in code, is materially wired into the current execution path, and is not just a placeholder.
- `[Partial]` exists, but is thin, heuristic, local-only, or materially below the target design.
- `[Missing]` does not exist in a meaningful operational sense, even if the spec mentions it or the code has a placeholder.

## How To Use This File

- This file is meant to keep future agents honest about what Agintor is today versus what it is trying to become.
- If you widen the mutable runtime surface, update `runtime_manifest.json`, `agintor/prompt_builder.py`, the baseline runtime policies, boundary tests, and this file.
- If you change runtime profile semantics, update `agintor/runtime_profile.py`, the baseline `runtime_profile.json`, loader identity hashing, evaluator cache assumptions, and the runtime-identity tests.
- If you add or change provider behavior, update `agintor/providers.py`, provider-specific modules, environment isolation in `agintor/runner.py`, Docker forwarding in `agintor/container_runtime.py`, and provider tests.
- If you add a new benchmark family, update the suite builder, objective generation, family weighting assumptions, trace labeling, goal rubric heuristics, and the relevant tests.
- The test suite is part of the architecture contract here. Status claims should line up with the current tests, not just the spec prose.

## Core Identity

- Agintor is not a prompt-tuning project wrapped around a fixed agent.
- Agintor is a bounded evolutionary search system over executable runtime code.
- The search target is the code that decides topology, memory, tooling, and control.
- The mutable surface is intentionally narrow: four policy modules plus their contracted methods.
- The shell around that surface is meant to remain deterministic, safety-bounded, and replayable.
- Runtime identity is supposed to be a property of code plus profile, not of a hidden prompt.
- The desired product is a runtime factory: given a goal domain or benchmark pressure, Agintor should export a runtime artifact with behavior encoded in policy code.
- The current repository is still much closer to an MVP runtime-search workbench for synthetic structured tasks than to a production MAS-factory.

## Design Priorities

- Preserve the bounded mutable surface. If a change quietly widens shell mutability, it is probably architectural drift.
- Prefer changes that make runtime behavior more code-driven, replayable, and evaluator-visible rather than more prompt-driven or hidden.
- Prioritize stronger benchmark pressure, better invariants, and more meaningful selection signals over cosmetic feature growth.
- Treat durable tooling, resume fidelity, memory discipline, and calibrated control as core product systems, not polish.
- Prefer simplifying and hardening existing subsystems over adding new speculative surfaces that the evaluator cannot measure.
- If a change helps only the demo suite but weakens generality, determinism, or boundary clarity, it is probably the wrong trade.
- The near-term point of the repo is to make runtime search credible on harder tasks; the long-term point is to export robust domain runtimes, not just to evolve demo heuristics.

## What The Repository Already Is

- `[Implemented]` The repo already contains the full MVP search loop: runtime scaffolding/loading/profile resolution, task runner, staged evaluator, evolution engine, archive, mutators, crossover, goal-conditioned builder, provider layer, Docker backend, and CLI.
- `[Implemented]` The baseline runtime template is real and runnable: manifest, embedded profile, and four mutable policy files with enforced method contracts.
- `[Implemented]` Runtime identity, provider isolation, predictor observation extraction, goal-conditioned export, and many architectural boundary tests are wired into the current implementation.
- `[Implemented]` The test suite exercises a meaningful part of the contract surface: clone-on-run, graph safety, patch integrity, runtime identity, async handles, provider behavior, archive insertion, and builder selection.
- `[Partial]` What exists is a coherent MVP runtime-search workbench, not a production MAS-factory.

## Product Architecture In One Flow

```text
CLI
-> load suite + runtime manifest + effective profile + provider
-> evaluator or builder
-> FixedShell + TaskRuntime
-> topology/memory/tool/control policies
-> tools + memory + verifier checks + traces
-> scoring
-> archive / validation / export
```

- `[Implemented]` That flow is real and end-to-end runnable.
- `[Partial]` The task model currently arrives pre-structured as benchmark operations, so the runtime is optimizing execution strategy around structured tasks rather than inventing arbitrary task graphs from raw goals.

## Runtime Artifact Model

- `[Implemented]` A runtime is a local directory artifact with a manifest, an embedded runtime profile, four mutable policy modules, immutable shell dependencies, and profile-sensitive hashing.
- `[Implemented]` Runtime identity changes when policy code or the effective profile changes, and evaluator cache keys respect that identity.
- `[Partial]` The runtime artifact is still a loose local Python directory, not a sealed packaged runtime with a compatibility contract.
- `[Partial]` The runtime profile strongly shapes execution, but it is not currently part of the mutable search surface.
- `[Missing]` There is no explicit runtime ABI/version handshake between exported runtimes and the shell/runtime API.
- `[Missing]` There is no signed provenance bundle, no reproducible export manifest, and no artifact attestation story.
- `[Missing]` There is no packaged durable asset layer for promoted tools, memory snapshots, benchmark adapters, or environment metadata.

## Mutable Genotype

- `[Implemented]` The four mutable policy modules cover topology, memory, tooling, and control, and mutations are constrained to contracted methods inside them.
- `[Implemented]` Search can both patch those methods and transplant selected ones through AST-level crossover.
- `[Partial]` These policies are still handwritten heuristic controllers driven mostly by profile weights, lexical overlap, and simple counters.
- `[Partial]` Evolution searches only the four policy files and does not co-evolve the runtime profile, benchmark adapters, or shell internals.
- `[Partial]` The current search space is still mostly line-local heuristic adjustment, not broader self-programming logic.
- `[Missing]` There is no richer mutable helper-surface library or internal DSL for policies beyond ordinary Python methods.
- `[Missing]` There is no mutation pressure for cleanup, simplification, code compression, or policy refactoring.
- `[Missing]` There is no learned or co-evolved family of runtime programs approaching the spec's intended decision sophistication.

## Fixed Shell

- `[Implemented]` `FixedShell` is the current immutable runtime substrate and already owns the agent pool, memory systems, board, handle table, safety guard, sandbox/tool subsystems, traces, and predictors.
- `[Implemented]` The shell resets task-local state, enforces transfer-memory boundaries, and hard-invalidates broken handle integrity, raw-evidence reachability, or cross-task memory leakage.
- `[Partial]` Shell immutability is enforced by the manifest and evaluator mutation boundary, not by a separately packaged binary/runtime boundary.
- `[Partial]` The shell is process-local and in-memory; it is not yet a durable orchestration substrate with persistence, resume services, or an event store.
- `[Missing]` There is no first-class checkpoint/resume manager that can restore open handles, board state, and suspended branches.
- `[Missing]` There is no persistent shell state store, replay database, or observability surface for long-running runtime evolution.
- `[Missing]` There is no hardened sandbox/security manager beyond Python-level validation and the thin Docker runner.

## Provider Boundary

- `[Implemented]` Local deterministic and hosted-provider execution paths are separated, with shared provider infrastructure plus OpenAI and MiniMax adapters wired through runtime profiles.
- `[Implemented]` Runtime execution scrubs unrelated provider environment variables to reduce cross-provider leakage during a run.
- `[Partial]` The provider abstraction is still text-generation-centric; it does not expose tool calling, batching, streaming control, structured retries, or failover.
- `[Missing]` There is no provider health-check layer, rate-limit handling strategy, failover routing, audit trail, or offline replay stub for hosted generations.

## Benchmarks And Task Model

- `[Implemented]` The current benchmark model already supports train/val/test/proxy partitions, family labels, context items, verifier types, structured operations, and transfer-scored episodes.
- `[Implemented]` The shipped demo suite exercises `top`, `mem`, `tool`, and `e2e` paths plus trace-focused proxies.
- `[Partial]` The benchmark suite is still tiny, synthetic, hard-coded in Python, and heavily structured around predeclared operations.
- `[Partial]` Many tasks already tell the runtime what operations exist, so the benchmark pressure is still much narrower than real-world open-ended MAS work.
- `[Missing]` There is no benchmark DSL, plugin adapter system, domain-specific task loader layer, or realistic external-task integration.
- `[Missing]` There are no serious repo-editing, browser, external-service, multimodal, or long-horizon benchmark families yet.

## Agent System

- `[Implemented]` The runtime already has clone-on-run canonical agents, child specs, isolated worker frames, deterministic merge rules, and a minimal message board.
- `[Implemented]` Worker isolation snapshots runtime state deeply enough that branches do not share mutable execution state during the current MVP runner.
- `[Partial]` Horizontal workers are logically parallel only. They currently execute sequentially in-process, not concurrently.
- `[Partial]` The message board is still mostly a contract placeholder. Workers do not yet use it as a meaningful coordination channel.
- `[Partial]` Ephemeral agent creation is still shallow `AgentTemplate` instantiation, not durable runtime-owned agent programming.
- `[Missing]` There is no true concurrent scheduler, cancellation/preemption system, branch-level budget manager, or durable inter-agent protocol layer.
- `[Missing]` There are no evolving agent classes, no persistent specialization state, and no role-specific lifecycle management beyond template cloning.

## Short-Term Memory

- `[Implemented]` Short-term memory is an append-only execution graph with typed nodes/edges, summary replacement, backlinks, reachability checks, and child-summary republishing.
- `[Partial]` The graph is still a lightweight in-memory execution record, not a rich queryable provenance system.
- `[Partial]` The full spec vocabulary exists, but the runtime currently exercises only a subset of the deeper graph semantics.
- `[Missing]` There is no persisted short-term provenance store, no replay explorer, no trace query API, and no diff/debug tooling over compaction behavior.
- `[Missing]` There is no robust resume reconstruction path from graph state back into live execution state.

## Long-Term Memory

- `[Implemented]` Long-term memory already supports typed memory nodes, exact symbol/path priority, simple neighborhood expansion, policy-driven promote/dedup/upsert behavior, and non-transfer task isolation.
- `[Partial]` The long-term store behaves more like a typed retrieval cache than like the richer graph-memory system described by the spec.
- `[Partial]` Embeddings are cheap lexical hash embeddings, not serious semantic retrieval infrastructure.
- `[Partial]` Dedup/upsert supports `merge`, `refine`, and `tombstone` paths in principle, but current policy behavior mostly exercises `merge` or `new`.
- `[Missing]` There is no persisted cross-run knowledge base, no explicit memory-edge graph, no contradiction-resolution system, and no retrieval diagnostics surface.
- `[Missing]` There is no strong notion of reusable abstractions, procedures-with-provenance, or environment fingerprints extracted from real execution.

## Tooling System

- `[Implemented]` The current tool stack already covers category-first discovery, built-in reusable tools, expression-based synthesis, content-addressed materialization, safety/validation, sync+async execution, task-local generated-tool reset, and tool-failure recording.
- `[Implemented]` Async launch now works for both source-backed tools and executor-backed backgroundable tools.
- `[Partial]` Tool synthesis is still mostly expression-driven Python code generation, not a broader tool-construction ecosystem.
- `[Partial]` Async execution exists, but the runner usually awaits the handle immediately, so the system does not yet exploit meaningful overlap.
- `[Partial]` Tool promotion thresholds exist, but promotion does not create a durable reusable registry that survives task resets or export.
- `[Partial]` Generated tools are still effectively task-local, so distinct-task reuse is measured but not operationalized into durable evolution.
- `[Missing]` There is no serious dependency-managed multi-runtime tool builder beyond Python source materialization.
- `[Missing]` There is no reusable promoted-tool registry with versioning, rollback, provenance, export packaging, or sharing across runtimes.
- `[Missing]` There is no large permissioned tool ecosystem, no external service tool layer, and no stateful durable tool substrate.
- `[Missing]` There is no production-grade sandbox policy enforcement with resource quotas, secret isolation, network allowlists, syscall/process controls, or audit logs.
- `[Missing]` There is no background-job lifecycle management for retries, cancellation, orphan cleanup, or crash recovery.

## Control System

- `[Implemented]` The control layer already covers model-class assignment, checker selection, limited escalation, stop policy, and runtime budget accounting.
- `[Partial]` This is still a hand-authored VOI-style controller, not the predictor-backed control surface from the spec.
- `[Partial]` Model assignment currently matters mostly for bookkeeping and auxiliary prompt calls. The runtime still lacks a genuine general-purpose model-driven reasoning loop.
- `[Partial]` Confidence, uncertainty, and terminal utility are still simplified approximations rather than calibrated runtime estimates.
- `[Missing]` There is no predictor-driven runtime control path using conservative and optimistic bounds during task-time decisions.
- `[Missing]` There is no branch-level budget allocation, rollback planning, or explicit action-value accounting across concurrent branches.

## Inner-Loop Task Execution

- `[Implemented]` `TaskRuntime.run_task()` is the real runtime state machine: it resets shell state, ingests task context, executes single/vertical/horizontal flows, verifies outputs, records traces, and returns scored run results.
- `[Implemented]` Tool-hint compatibility now uses parsed signature arguments rather than loose substring matching.
- `[Partial]` The runtime is still operating on benchmark-specified operation graphs rather than synthesizing rich plans from unstructured goals.
- `[Partial]` Snapshot/restore for isolated branches is still ad hoc and mutates private internals of the shell subsystems.
- `[Partial]` Checkpoints exist as objects and summaries, but there is no actual restart-from-checkpoint execution path.
- `[Missing]` There is no true suspend/resume workflow, no durable continuation of long-lived async branches, and no generalized planner/model execution loop.
- `[Missing]` There is no robust recovery taxonomy separating invalidation, verifier failure, provider failure, transient tool failure, and resumable branch failure.

## Evaluation And Scoring

- `[Implemented]` The evaluator already supports local or Docker execution, profile-aware runtime loading, repeated seeds, transfer-scored task grouping, staged gates, full-train early rejection, and validation-only leader checks.
- `[Implemented]` `ScoreCalculator` computes task, family, and global objectives including robustness-adjusted scores and lower-tail risk.
- `[Implemented]` Transfer-scored episodes are preserved during full-train batching instead of being split across stage-4 minibatches.
- `[Partial]` Stage 0 is stricter than a simple parse check, but it still does not run formatter/linter/unit-test gates beyond AST and boundary validation.
- `[Partial]` The staged evaluator is structurally aligned with the spec, but the benchmark universe is still a tiny demo suite.
- `[Partial]` The verifier stack is still synthetic and exact-match or trace-proxy oriented, not the broader family of benchmark-specific graders the full system will need.
- `[Partial]` Docker evaluation isolates whole runtime units, not per-tool execution or benchmark-specific sandbox policies.
- `[Missing]` There is no serious benchmark adapter ecosystem, no contamination-governed held-out program, no distributed evaluation harness, and no experiment database.
- `[Missing]` There is no production-grade grader family for repos, browsers, services, or long-horizon workflows.

## Evolution Loop

- `[Implemented]` The evolution loop already has objective-conditioned parent selection, a phased scope scheduler, heuristic and provider-backed patch mutation, AST crossover, staged child evaluation, QD archive insertion, validation ticks, and counterfactual scope-credit updates.
- `[Implemented]` Predictor observations are extracted from fully evaluated runs and fed back into the search loop.
- `[Partial]` The heuristic mutator is still a narrow hard-coded search over a few local replacements.
- `[Partial]` The provider-backed mutator is still constrained to small local patches inside four files.
- `[Partial]` Objective selection is uniform random rather than adaptive to archive state, uncertainty, or diminishing returns.
- `[Partial]` Evolution state is mostly in-memory plus `evolution_history.json`; resumable long-running search is underbuilt.
- `[Missing]` There is no operator portfolio adaptation, no search checkpoint/restart system, no multi-parent crossover, and no distributed island execution.
- `[Missing]` There is no dedicated simplification/refactoring operator that can reduce heuristic cruft while preserving behavior.

## Predictors

- `[Implemented]` The predictor layer already has a model bank, trace-derived observations, periodic retraining, eval-time freezing, and mutation-prompt summaries.
- `[Partial]` The predictors are not yet deeply consumed by the runtime policies themselves.
- `[Partial]` Feature vectors are still simple trace/count summaries rather than rich runtime-state features.
- `[Partial]` The spec envisions conservative and optimistic utilities throughout topology, tooling, memory, and control; current code trains models but barely routes decisions through them.
- `[Missing]` There is no meaningful calibration/monitoring loop, no uncertainty diagnostics surface, and no deep predictor integration into task-time policy decisions.

## Goal-Conditioned Runtime Builder

- `[Implemented]` `build-runtime` is a separate product path: it derives rough goal families from a prompt, clones representative demo tasks into a goal-conditioned suite, runs evolution, and exports by goal-score-first then validation tie-break.
- `[Implemented]` Exported runtimes keep their own embedded runtime-provider profile instead of inheriting the builder's provider choice.
- `[Partial]` This is an MVP approximation of runtime synthesis from intent, not a true goal-to-runtime compiler.
- `[Partial]` The builder currently clones demo benchmark tasks with goal metadata rather than generating goal-native evaluation pressure.
- `[Partial]` Goal-family inference is keyword heuristic and shallow.
- `[Missing]` There is no goal-to-benchmark compiler, no domain-specific verifier generation, no acceptance-test synthesis, and no runtime deployment packaging.

## Docker And Runtime Isolation

- `[Implemented]` The evaluator can run runtimes locally or inside Docker, and provider environment forwarding is scoped to the selected provider family.
- `[Partial]` Docker isolation is useful but still thin compared with the spec's stronger sandbox and environment-control ambitions.
- `[Partial]` The container image is repo-wide and evaluation-wide, not a hardened per-tool or per-branch sandbox.
- `[Missing]` There is no network restriction, capability dropping, cgroup/timeout enforcement at the container level, or attested runtime environment story.

## Non-Negotiable Architectural Invariants

- `[Implemented]` The mutable surface is only the four policy files named in the manifest.
- `[Implemented]` Mutations may only alter the contracted mutable methods inside those files.
- `[Implemented]` The shell boundary is supposed to remain immutable during evolution.
- `[Implemented]` Canonical agents must be cloned before execution.
- `[Implemented]` Generated tools must pass safety and validation checks before use.
- `[Implemented]` Short-term compaction must preserve backlinks to raw evidence.
- `[Implemented]` Open-handle table integrity is a hard invalidation condition.
- `[Implemented]` Long-term memory must reset across normal tasks and only persist inside explicit transfer-scored episodes.
- `[Implemented]` Category-first tool discovery is mandatory.
- `[Implemented]` Tool hints may not bypass category-first discovery.
- `[Implemented]` Exact symbol or file-path retrieval must outrank fuzzy memory retrieval.
- `[Implemented]` Runtime identity must include the effective runtime profile, not just the policy files.
- `[Implemented]` Validation traces and test traces must stay out of mutation prompts.
- `[Implemented]` Merge order for worker outputs must be deterministic.

## Current State Of Completion

- `[Implemented]` The project already proves the bounded-runtime-search architecture end to end: scaffold runtime, execute tasks, evaluate, evolve, archive, and export.
- `[Implemented]` The test suite enforces many of the core runtime and search invariants, and the baseline runtime solves the current demo suite.
- `[Partial]` The present code is still an MVP shell-plus-baseline-runtime for small synthetic structured tasks, not yet a benchmark-grade runtime search platform.
- `[Partial]` The architecture is worth building on, but the current behaviors prove the scaffolding more than they prove deep agent capability.
- `[Missing]` The project does not yet demonstrate robust domain-specialized runtimes that clearly and repeatedly outperform the seed runtime on large held-out suites.

## Major Gaps That Still Matter

- `[Partial]` Topology, memory, tooling, and control are separated correctly, but they are still mostly heuristic policies rather than genuinely co-evolved, predictor-driven runtime programs.
- `[Partial]` The runtime does not yet do rich model-driven planning over open-ended tasks; it mostly executes benchmark-specified operation graphs.
- `[Partial]` Checkpoint/resume, durable tool promotion, richer long-term memory, and goal-conditioned building all exist only in thin MVP form.
- `[Missing]` The system still lacks realistic benchmark pressure, production-grade durability/isolation, and predictor-driven task-time decisions.
- `[Missing]` The project still lacks evidence of robust domain-specialized runtime improvement on serious held-out suites.

## Additional Missing Systems That Separate This Repo From A Production MAS-Factory

- `[Missing]` A runtime ABI/versioning story for exported runtimes, shell compatibility, and forward/backward migration.
- `[Missing]` A durable promoted-tool registry with provenance, versioning, rollback, sharing, and export integration.
- `[Missing]` A real multi-runtime tool builder supporting package installation, multiple languages, stateful services, and reproducible build environments.
- `[Missing]` A hardened sandbox stack with process isolation, filesystem policies, secret handling, network controls, quotas, and audit logs.
- `[Missing]` A durable checkpoint/resume system that can restore board state, open handles, unresolved goals, branch queues, and verifier state after process death.
- `[Missing]` Real concurrent execution for workers, background tools, and long-lived branches instead of mostly sequential in-process simulation.
- `[Missing]` A richer long-term memory graph with explicit edges, contradiction handling, provenance reasoning, versioning, and retrieval debugging.
- `[Missing]` Provenance-grade replay tooling for short-term traces, summary replacement, and execution diffs across candidate runtimes.
- `[Missing]` A benchmark/plugin ecosystem for repo tasks, browser tasks, service orchestration, multimodal tasks, and long-horizon episodes.
- `[Missing]` Strong verifier families and benchmark-specific graders that make archive scores meaningful outside the synthetic demo suite.
- `[Missing]` Predictor-driven task-time decision making with calibrated uncertainty and conservative/optimistic action routing.
- `[Missing]` Distributed search execution, resumable archive state, experiment tracking, lineage browsing, and champion promotion/rollback workflows.
- `[Missing]` Runtime export packaging that includes deployment metadata, environment fingerprints, provider contracts, and reusable assets.
- `[Missing]` Goal-native benchmark synthesis and domain-conditioned acceptance criteria rather than cloned benchmark tasks with prompt emphasis.
- `[Missing]` Observability surfaces for runtime traces, tool lifecycle, memory promotion, predictor drift, and archive evolution.
- `[Missing]` A deployment story for produced runtimes: registry, artifact promotion, rollback, compatibility validation, and runtime-health monitoring.

## What Ought To Be Added, Improved, Simplified, Or Refactored Next

- The highest-leverage addition is a real open-ended planner loop. Right now the runtime is mostly deciding how to execute benchmark-provided operations, not generating rich plans from raw goals.
- The highest-leverage durability refactor is to make checkpoint/resume first-class. `Checkpoint` should stop being a summary object only and become a restorable execution artifact.
- The tooling subsystem should be split into clearer layers: discovery, synthesis, validation, registry, execution, and promotion. Promotion needs to stop being trace-only and start becoming a durable asset pipeline.
- Worker isolation should move away from ad hoc deep-copying of private subsystem internals. Introduce explicit snapshot/restore APIs for shell state, predictors, tool registry state, and memory state.
- Provider use should be normalized around real request objects instead of several ad hoc `type("Req", ...)` constructions in policies.
- Benchmarks should be pulled out of the hard-coded demo-task shape into an adapter/plugin layer with stronger task metadata, graders, and transfer episodes.
- Predictor infrastructure should stop being mutation-prompt-only. The next architectural step is routing actual topology, memory, tooling, and control decisions through those predictors.
- Runtime artifacts should gain an explicit ABI/version contract and an export bundle format before the project claims to be a true runtime factory.
- Security and isolation should be designed as core product systems, not as post-hoc wrappers. This repo is currently far too permissive to support a production self-programming MAS-factory.
- The shell should stay small and deterministic. New experimentation should land in policy code, provider-neutral adapters, and benchmark/plugin layers, not by quietly widening shell mutability.

## End-State This Repository Should Reach

- Agintor should become a runtime search platform that can take a goal domain, construct or select the right benchmark pressure, evolve within a bounded policy surface, and export a deterministic runtime artifact.
- That exported runtime should have a fixed shell, strong invariants, explicit profile, durable tool strategy, disciplined memory policy, calibrated control policy, and a compatibility-checked artifact boundary.
- The runtime should improve because its code changed, not because a hidden prompt changed.
- The evaluator should be strong enough that archive scores mean something beyond synthetic demo success.
- The archive should preserve diverse competent runtimes, not just one tuned heuristic.
- The builder should ultimately be able to turn intent into a benchmark program, an evolved runtime family, and a deployable runtime artifact rather than just run a short search over cloned demo tasks.
