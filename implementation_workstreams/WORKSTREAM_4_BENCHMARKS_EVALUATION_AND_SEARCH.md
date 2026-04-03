# Workstream 4: Benchmarks, Evaluation, and Search

## Outcome

- Factory evaluation consumes frozen `BenchmarkPlan` and `VerifierBundle` artifacts through the same runtime entrypoint used by exported-runtime solve execution.
- Benchmark pressure graduates from demo-shaped structured tasks to serious local task families that can actually justify runtime claims.
- Evaluation becomes a durable factory surface with stage-failure ledgers, validation history, leaderboard snapshots, contamination tracking, and held-out reports.
- Search becomes resumable and auditable, with persisted archive state, scheduler state, operator history, and deterministic reporting.
- MVP success is demonstrated by held-out improvement on serious bounded suites, not by archive mechanics alone.

## Prerequisites

- Workstream 1 freezes the build artifact chain and host/runtime boundary.
- Workstream 2 freezes solve-time execution semantics.
- Workstream 3 provides durable run, checkpoint, and replay artifacts.

## Sequence Position

- This workstream starts after Workstream 1 freezes planning artifacts, Workstream 2 freezes runtime execution semantics, and Workstream 3 provides a durable runtime-state substrate.
- Workstream 5 depends on held-out reports and stage-failure ledgers from this workstream before it upgrades runtime-side tooling and control policy.

## Boundaries

- Own benchmark content, typed task adapters, verifier catalogs, staged evaluation, robustness measurement, contamination control, search policy, search-state persistence, validation reporting, and held-out reporting.
- Keep runtime-state semantics, orchestration mechanics, tool sandbox internals, provider transport behavior, and solve-time policy implementation outside this workstream.
- Keep open-ended benchmark invention, free-form grader generation, and internet-dependent benchmark lanes outside the MVP lane.

## Planning Constraints

- `BenchmarkPlan` and `VerifierBundle` are the only legal evaluation inputs once frozen.
- Later stages must not silently reparse the original goal or rebuild suites from scratch.
- Fixture setup and environment digests must be frozen separately from task selection.
- MVP proof lanes stay bounded:
  - first serious lane: `repo_patch`
  - second serious lane: `service_task`
  - `browser_task` lands as scaffold, not the first gating lane
  - `multimodal_task` stays placeholder-only until the rest of the pipeline is stable

## Baseline

- `agintor/benchmarks.py` already supports suites with `train`, `proxy`, `val`, and `test` partitions.
- `agintor/verifiers.py` already supports exact and near-exact local verification plus the `local`, `subtree`, `repo`, and `benchmark` checker ladder.
- `agintor/evaluator.py` already has the right staged-evaluation skeleton: patch integrity, deterministic smoke, proxy gate, local subset gate, and full-train gate.
- `agintor/archive.py` and `agintor/evolution.py` already implement objective islands, scope scheduling, staged acceptance, and predictor updates.
- The current suite is still dominated by small structured tasks, and evaluation is stronger than the benchmark pressure it measures.
- Search state is still too process-local, and objective or operator policy is still too simplistic for the optimizer machinery already present.

## Reference Benchmark Ladder

- `structured_ops`: deterministic local computational and structured-output pressure
- `repo_patch`: repository editing, patch application, and local test execution under frozen fixtures
- `service_task`: bounded stateful tool or service workflows with deterministic fixture state transitions
- `browser_task`: scaffolded DOM or state assertions under frozen local fixtures
- `multimodal_task`: placeholder only until the rest of the runtime and evaluator stack are stable

## Core Decisions

- Treat frozen `BenchmarkPlan` and `VerifierBundle` as the only legal evaluation inputs.
- Measure runtimes through the runtime entrypoint, not through hidden direct imports.
- Keep benchmark growth bounded and locally judgeable.
- Make repo editing the first serious proof lane.
- Make stateful service tasks the second serious proof lane.
- Build `browser_task` support in the adapter registry, but do not make browser flows an MVP gating lane before repo and service tasks are stable.
- Keep `multimodal_task` as a later adapter slot, not an MVP proof target.
- Add a simplification or refactoring operator to the search portfolio so the system can improve by getting smaller and cleaner, not only by getting more elaborate.
- Fix the signal bottleneck explicitly. Search policy must preserve enough full-train and held-out evidence to justify archive, scheduler, and predictor complexity.

## Phase 1: Make Frozen Planning Artifacts Authoritative

- Make `BenchmarkPlan` and `VerifierBundle` the only legal inputs to:
  - `runtime_builder.py`
  - `evaluator.py`
  - `evolution.py`
- Remove hidden suite reconstruction from later stages.
- Route all evaluation through the runtime execution entrypoint created in Workstream 2.
- Persist at least:
  - `validation_history.json`
  - `stage_failures.json`
  - `leaderboard.json`
  - `evolution_history.json`
  - `benchmark_provenance.json`
- Record enough provenance to rerun evaluation from disk without reopening the raw goal prompt or implicitly rebuilding the suite.

## Phase 2: Build a Typed Benchmark Adapter Registry

- Add a typed adapter registry in `agintor/benchmarks.py` for at least:
  - `structured_ops`
  - `repo_patch`
  - `service_task`
  - `browser_task`
  - `multimodal_task`
- Separate fixtures from selection. `BenchmarkPlan` must reference:
  - task IDs
  - fixture IDs
  - environment digests
  - verifier IDs
  - contamination flags
  - provenance fields
- Keep benchmark planning conservative:
  - select existing tasks first
  - clone or adapt bounded templates second
  - synthesize only when deterministic local grading remains possible
- Require every benchmark family to declare why it is locally judgeable and reproducible.

## Phase 3: Expand Verifiers into Typed Local Graders

- Replace loose verifier naming with serialized `VerifierSpec` objects.
- Add typed local verifier families for:
  - patch applicability
  - repository test execution
  - diff-shape constraints
  - service-state transitions
  - artifact-schema checks
  - milestone checks
  - browser assertions later
- Keep verifier creation template-driven and typed. Do not allow free-form provider-authored grading code into the MVP evaluation path.
- Persist replayable verifier evidence for every serious verifier family.
- Keep validation and test verifier outputs mutation-invisible.

## Phase 4: Harden Evaluation, Held-Out Policy, and Contamination Control

- Add family-specific held-out rules:
  - fixed train, validation, and test for deterministic local tasks
  - freshness-controlled held-out lanes for repo tasks where feasible
  - environment fingerprints for service and browser tasks
  - contamination flags for cloned or synthesized descendants
- Persist stage-failure rows with:
  - stage
  - candidate hash
  - touched scope
  - operator type
  - benchmark refs
  - verifier refs
  - failure reason
  - rerun eligibility
- Persist validation history as a ledger rather than as an internal tie-break artifact.
- Add held-out report artifacts that tie runtime claims to exact benchmark and verifier inputs.

## Phase 5: Make Search State Resumable and Operator Policy Deliberate

- Persist search state including:
  - archive
  - scope scheduler
  - objective history
  - predictor snapshots
  - current phase budgets
  - leader set
  - RNG state
  - operator history
  - lineage
- Replace uniform-random objective selection with deterministic scoring over:
  - archive under-coverage
  - family underrepresentation
  - uncertainty
  - stagnation age
  - recent acceptance rate
- Add an operator portfolio manager over:
  - heuristic mutation
  - provider-assisted mutation
  - crossover
  - simplification or refactoring
- Record why each operator and objective were chosen.

## Phase 6: Fix the Evaluation-Signal Bottleneck

- Keep the staged evaluator, but make signal sufficiency an explicit requirement.
- Persist full-train and held-out evidence often enough that:
  - archive credit is not driven only by a tiny handful of Stage 4 survivors
  - predictor families can be retrained on meaningful fully evaluated samples
  - stage tightening does not silently starve the optimizer
- Add reporting that exposes:
  - Stage 0 to Stage 4 pass rates
  - full-train evaluation counts
  - accepted-elite counts
  - predictor retraining triggers
  - objective coverage by family and scope
- If the search loop is not generating enough fully evaluated evidence to support the current archive and predictor design, the search policy must tighten scope, simplify objectives, or expand the serious proxy layer instead of pretending the signal is sufficient.

## Phase 7: Run MVP Proof Campaigns on Serious Suites

- Freeze before long runs:
  - baseline runtime
  - benchmark plan
  - verifier bundle
  - seed budget
  - held-out criteria
  - reporting format
- Make `repo_patch` the first serious proof lane.
- Make `service_task` the second serious proof lane.
- Keep `browser_task` implemented as a scaffolded adapter, but do not gate MVP claims on it.
- Require every exported leader claim to cite:
  - benchmark plan ID
  - verifier bundle ID
  - held-out report ID
  - runtime hash
- Report by family:
  - train score
  - validation score
  - held-out score
  - robustness
  - cost
  - latency
  - fault rate
  - verifier coverage
  - checkpoint or resume stability where relevant

## Regression Gates

- Add tests proving:
  - evaluation reruns from frozen disk artifacts
  - typed adapters resolve fixtures deterministically
  - verifier evidence bundles replay cleanly
  - contamination flags are preserved
  - validation and test traces never enter mutation context
  - search state resumes consistently
  - operator choice and lineage are logged
  - leader export references the correct held-out report
- Keep all default evaluation tests offline and deterministic.

## Handoff to Workstream 5

- Workstream 5 receives:
  - typed benchmark families
  - typed verifier evidence
  - durable evaluation ledgers
  - contamination controls
  - resumable search state
  - serious held-out reports
- Workstream 5 must use that evidence to decide which tooling, provider, and control decisions deserve runtime-owned contracts and predictor-backed policies.

## Acceptance Gates

1. `build-runtime` and `evolution` consume frozen `BenchmarkPlan` and `VerifierBundle` artifacts rather than reconstructing suites implicitly.
2. Evaluation runs through the runtime entrypoint rather than hidden direct-import execution paths.
3. The workspace contains durable evaluation artifacts, including validation history, stage failures, leaderboard snapshots, and benchmark provenance.
4. At least one serious non-demo benchmark family is active with typed fixtures and typed local verifiers.
5. Search can resume from persisted archive and scheduler state without rebuilding the world from scratch.
6. Search reporting exposes whether the optimizer has enough full-train evidence to justify its current complexity.
7. An exported leader is backed by a reproducible held-out report on a serious suite, not only by structured-demo improvements.

## File Ownership

- `agintor/benchmarks.py`: adapter registry, fixture references, benchmark provenance
- `agintor/verifiers.py`: typed verifier catalog and evidence serialization
- `agintor/evaluator.py`: staged evaluation, failure ledgers, validation history, held-out policy
- `agintor/archive.py`: archive descriptors and persisted archive state
- `agintor/evolution.py`: objective selection, operator portfolio, resume support, leader tracking
- `agintor/mutator.py`: simplification and refactoring operator hooks
- `agintor/crossover.py`: bounded whole-method crossover
- `agintor/predictors.py`: predictor snapshot persistence and search-side summaries
- `tests/test_runtime_builder.py`, `tests/test_evolution.py`, `tests/test_benchmarks_plugin.py`, and adjacent new tests: benchmark, verifier, contamination, and search-resume regression coverage

## Deferred

- Open internet benchmark lanes
- Free-form grader generation
- Cloud-first distributed search as the primary lane
- Multimodal proof campaigns before repo and service lanes are stable
