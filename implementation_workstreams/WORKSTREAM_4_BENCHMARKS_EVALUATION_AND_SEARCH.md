# Workstream 4: Benchmarks, Evaluation, And Search

## Outcome

- `build-runtime` must evolve against a persisted `BenchmarkPlan` and `VerifierBundle`, not an in-memory demo suite assembled ad hoc inside the build path.
- Benchmark pressure must grow from toy structured tasks into a bounded ladder of serious families: repo editing first, then browser workflows, then stateful tool and service tasks, then multimodal and longer-horizon tasks.
- Evaluation must become an inspectable factory surface with frozen benchmark provenance, stage-failure reporting, validation history, leaderboard snapshots, and contamination-controlled held-out measurement.
- Search must remain bounded but become operationally usable: resumable state, adaptive objective and operator selection, simplification and refactoring operators, and optional distributed execution after the local deterministic lane is stable.
- MVP success is demonstrated by measurable improvement on held-out serious suites, not by archive mechanics alone.

## Boundaries

- Own benchmark and verifier content, benchmark/task adapters, staged evaluation, validation reporting, held-out policy, archive and search policy, and search-state persistence.
- Workstream 1 owns the schema shapes and workspace placement for `BenchmarkPlan`, `VerifierBundle`, `build_summary.json`, `leaderboard.json`, and related planning outputs. This workstream owns the contents of those artifacts and the code that consumes them during evaluation.
- Workstream 2 owns runtime execution semantics, task-time orchestration, and shell invariants. This workstream may require new telemetry or adapter hooks from the runtime host, but it does not own the host itself.
- Workstream 3 owns memory-specific benchmark pressure once transfer, retrieval, and compaction durability require new runtime-state semantics or memory-graph behavior.
- Workstream 5 owns provider integration, task-time predictor semantics, tool/runtime sandboxing, and control-surface behavior. This workstream owns only the factory-side use of predictors for evaluation, retraining cadence, freezing, and search decisions.

## Planning Constraints

- Keep MVP benchmark growth bounded and locally judgeable. Do not introduce open-ended benchmark synthesis or unverifiable graders in v1.
- Prefer typed task templates and frozen verifier templates over free-form provider-authored graders.
- Search-state hardening follows evaluation hardening. Do not scale out a search loop that still measures mostly synthetic toy tasks.
- Validation and test traces remain mutation-invisible at every phase.
- Determinism claims must match the stack. Hosted-provider and Docker paths may exist, but the reference evaluation lane stays local, replayable, and reproducible.

## Current Baseline

- `agintor/benchmarks.py` already defines `BenchmarkSuite` with `train`, `val`, `test`, and `proxy` partitions, task lookup helpers, JSON loading, and plugin or module suite providers.
- `agintor/schemas.py` already gives `BenchmarkTask` support for `context_items`, `file_paths`, `operations`, `proxy_scope_tags`, `transfer_scored`, `episode_id`, and `episode_order`.
- `agintor/verifiers.py` already supports `json_exact`, `json_numeric`, `string_exact`, `number_exact`, `trace_event`, and `trace_event_count`, plus the `local`, `subtree`, `repo`, and `benchmark` checker ladder.
- `agintor/evaluator.py` already enforces Stage 0 through Stage 4 gates, deterministic smoke replay, common-random-number comparisons, reference-scale estimation, robustness scoring, CVaR tie-breaks, minibatch early rejection, and validation evaluation.
- `agintor/archive.py` and `agintor/evolution.py` already implement objective catalogs, scope scheduling, archive insertion, counterfactual singleton and pair credits, AST crossover, predictor updates, and pass-rate tightening.
- `agintor/runtime_builder.py` still goal-conditions evaluation by cloning one representative demo task per family and appending prompt emphasis. There is no persisted `BenchmarkPlan` or `VerifierBundle` consumption path yet.
- Search state is still mostly process-local. The durable artifact today is `evolution_history.json`; there is no search checkpoint, validation history ledger, stage-failure report, or resumable archive snapshot.
- Objective sampling is still uniform random in `EvolutionEngine._select_objective()`.
- The active suite is still dominated by small synthetic structured tasks, so the evaluator is materially stronger than the benchmark pressure it currently measures.

## Reference Benchmark Ladder

- `Tier 1: Repo editing`
  - Target a SWE-bench Verified style shape: frozen repository snapshot, issue or task statement, patch artifact, and deterministic test-based grading.
- `Tier 2: Browser workflows`
  - Target BrowserGym-style adapters first, with bounded local environments and explicit success criteria before any open internet flows.
- `Tier 3: Stateful tool and service tasks`
  - Target ToolSandbox-style stateful scenarios with simulated services, intermediate milestones, and replayable state transitions.
- `Tier 4: Multimodal and longer-horizon tasks`
  - Add GAIA-style multimodal and tool-use pressure, and later bounded dynamic scenarios, only after the first three tiers are stable, locally judgeable, and contamination-controlled.

## Phase 1: Freeze Benchmark And Verifier Consumption

- Add concrete `BenchmarkPlan` and `VerifierBundle` loading and consumption paths so `agintor/runtime_builder.py`, `agintor/evaluator.py`, and `agintor/evolution.py` run from frozen planning artifacts instead of ad hoc suite assembly.
- Refactor suite construction so benchmark selection, cloning, bounded synthesis, and verifier freeze occur before any candidate evolution begins.
- Persist benchmark provenance with at least suite name, task source, fixture digest, environment digest, benchmark-plan ID, verifier-bundle ID, and generation timestamp.
- Add `validation_history.json`, `stage_failures.json`, and `leaderboard.json` outputs aligned with the target-spec workspace plan.
- Keep the content conservative in this phase. The demo suite may remain the only active content while the frozen-artifact contract lands.

`Exit gate:` a successful `build-runtime` run writes frozen benchmark and verifier artifacts, and the evaluator can be rerun from those artifacts without reopening the raw goal prompt or reconstructing the suite implicitly.

## Phase 2: Replace Toy Benchmark Assembly With Typed Adapters

- Introduce a typed benchmark-adapter registry in `agintor/benchmarks.py` for task families such as `structured_ops`, `repo_patch`, `browser_task`, `service_task`, and `multimodal_task`.
- Separate benchmark fixtures from benchmark selection. A benchmark plan should choose tasks and fixtures by ID, not embed loose task construction logic inside the build path.
- Add local fixture contracts for repository snapshots, browser environments, service simulators, and multimodal assets so every serious task has explicit setup and teardown requirements.
- Keep benchmark planning conservative: selection first, template cloning second, bounded synthesis last.
- Require every synthetic or adapted task to declare why its correctness is still deterministically and locally judgeable.

`Exit gate:` the benchmark plan can mix the current demo tasks with at least one serious non-demo family while still freezing all task IDs, fixtures, and verifier references before evolution starts.

## Phase 3: Expand The Verifier Ladder With Typed Local Graders

- Extend `agintor/verifiers.py` from compact artifact checks into a typed verifier catalog that includes patch validity, repository test execution, diff-shape checks, browser state assertions, service-state transitions, artifact-schema checks, and milestone verifiers.
- Add a serialized `VerifierSpec` layer so each benchmark task points to a frozen verifier contract instead of only a string verifier name.
- Keep verifier adaptation bounded. MVP verifier generation should be template-driven from typed contracts rather than free-form provider-written grading code.
- Add richer verifier evidence payloads so failures can be reported, replayed, and inspected without exposing validation or test traces to the mutator.
- Extend checker-ladder defaults by family so `local`, `subtree`, `repo`, and `benchmark` checks remain coherent when task types become more diverse.

`Exit gate:` repo tasks and one interactive family both have local deterministic graders, serialized verifier specs, and replayable verifier evidence.

## Phase 4: Harden Evaluation And Held-Out Measurement

- Add benchmark-family-specific held-out policies:
  - static train, validation, and test for local deterministic tasks,
  - hidden or private-answer lanes where feasible,
  - freshness-controlled or time-split held-out sets for repo tasks,
  - environment fingerprints for browser and service tasks.
- Persist stage-failure rows with stage number, candidate hash, touched scope, failure reason, and benchmark or verifier references.
- Persist validation history as a leaderboard ledger rather than only an internal tie-break calculation.
- Add explicit contamination flags and data lineage fields to benchmark provenance so benchmark reuse, cloning, and synthesis remain auditable.
- Introduce a worker-queue evaluation harness only after the single-node deterministic lane is stable. Keep family-specific environments isolated even when parallelized.

`Exit gate:` held-out evaluation can be rerun from disk with frozen inputs, validation history is inspectable, stage failures are queryable, and benchmark provenance makes train, validation, and test isolation auditable.

## Phase 5: Mature Search State And Operator Policy

- Persist the archive, scope scheduler, predictor snapshots, current phase budgets, leader set, and RNG state so evolution can checkpoint and resume.
- Replace uniform-random objective selection with archive-need, uncertainty, stagnation, and under-covered-family aware sampling.
- Add an operator portfolio manager that can choose among heuristic mutation, provider mutation, crossover, and a dedicated simplification or refactoring operator.
- Add champion promotion and rollback flows so validation winners and exported leaders are tracked explicitly instead of only copied at export time.
- Add lineage outputs that make parentage, touched scope, operator type, and acceptance reason inspectable from the workspace.
- Add distributed island execution only after checkpoint and resume are stable on a single machine.

`Exit gate:` interrupted evolution can resume from disk without losing archive or scheduler state, under-served objectives receive explicit search pressure, and the operator mix is inspectable in the workspace.

## Phase 6: Prove Improvement On Serious Held-Out Suites

- Choose two serious benchmark tracks for MVP proof, not all four. The recommended order is repo editing first, then either browser workflows or stateful service tasks.
- Define baseline, seed budget, held-out win criteria, and reporting format before running long search campaigns.
- Add CLI-readable reports that summarize per-family train score, validation score, held-out score, robustness, cost, latency, fault rate, and verifier coverage.
- Require exported-runtime claims to cite the exact benchmark plan, verifier bundle, and held-out report used to justify them.
- Do not treat benchmark-family expansion as complete until the seeded baseline and exported leader are compared on serious held-out tasks, not only on goal-conditioned demo clones.

`Exit gate:` a reproducible report shows that the exported leader beats the seeded runtime on at least one serious held-out suite while preserving deterministic evaluation and trace isolation rules.

## MVP Acceptance Sequence

1. `build-runtime` consumes persisted `BenchmarkPlan` and `VerifierBundle` artifacts instead of reconstructing the suite implicitly.
2. The workspace contains `benchmark_plan.json`, `verifier_bundle.json`, `validation_history.json`, `stage_failures.json`, `leaderboard.json`, and `evolution_history.json`.
3. At least one serious non-demo benchmark family is active with typed fixtures and local deterministic graders.
4. Validation and test traces remain excluded from mutation prompts and mutation heuristics after the new reporting surfaces land.
5. Evolution can resume from a persisted archive and scheduler snapshot without rebuilding the search state from scratch.
6. An exported leader is backed by a reproducible held-out report on a serious suite rather than only by synthetic demo-task scores.

## File Ownership

- `agintor/benchmarks.py`: suite registry, typed adapter loading, plan consumption, benchmark provenance, fixture references.
- `agintor/verifiers.py`: verifier catalog, checker-ladder defaults, verifier evidence serialization, typed local graders.
- `agintor/evaluator.py`: stage gates, stage-failure reporting, validation history emission, held-out isolation, evaluation-worker integration.
- `agintor/archive.py`: archive descriptors, objective islands, scope-scheduler persistence inputs, lineage metadata.
- `agintor/evolution.py`: objective selection policy, operator portfolio, checkpoint and resume, champion tracking, leaderboard writes.
- `agintor/mutator.py`: operator hooks for simplification, refactoring, and bounded family-aware mutations.
- `agintor/crossover.py`: whole-method crossover and donor-selection constraints.
- `agintor/predictors.py`: shared with Workstream 5; this workstream owns retraining cadence, freezing during evaluation, serialized snapshots, and search-side summaries only.

## Deferred Until Post-MVP

- Open-ended benchmark synthesis from raw goals without typed templates and frozen local verifiers.
- Open internet browser benchmarks as the default evaluation lane.
- Cloud-first or multi-cluster search as the primary execution mode.
- Public benchmark-hosting infrastructure or a public leaderboard service.
- Multimodal dynamic environments as the main MVP proof target before repo and one interactive family are stable.
