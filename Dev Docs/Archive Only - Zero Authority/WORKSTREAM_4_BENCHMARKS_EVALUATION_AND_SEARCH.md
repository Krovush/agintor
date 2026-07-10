# Workstream 4: Benchmarks, Evaluation, and Search

## Outcome

- Factory evaluation consumes frozen `BenchmarkPlan` and `VerifierBundle` artifacts through the same runtime entrypoint used by exported-runtime solve execution.
- Goal-conditioned benchmark planning becomes goal-scoped, coverage-driven, and provenance-rich instead of compressing rich goals into a tiny demo-shaped subset.
- Benchmark pressure graduates from demo-shaped structured tasks to serious local task families that can actually justify runtime claims.
- Evaluation becomes a durable factory surface built on Workstream 3 run lineage, checkpoint lineage, state indexes, grouped trace views, stage-failure ledgers, validation history, leaderboard snapshots, contamination tracking, and held-out reports.
- Search becomes resumable and auditable, with persisted archive state, scheduler state, operator history, predictor snapshots, RNG state, and deterministic reporting.
- MVP success is demonstrated by held-out improvement on serious bounded suites, not by archive mechanics alone.

## Inherited WS3 Context

Workstream 4 should be implemented against the current WS3 durability direction, not against older flat-run and flat-trace assumptions in legacy gap documents.

Relevant inherited context:

- `RunStore` remains the canonical run, attempt, checkpoint, request, receipt, and artifact authority.
- `agintor/state_store.py` provides rebuildable run-local indexes and query surfaces for checkpoint lineage, branch lineage, recovery outcomes, side-effect receipts, short-term provenance, long-term memory lineage, retrieval diagnostics, and run-local references to session-scoped hosted-call traces.
- `CheckpointEnvelope` is the v4 restart artifact with typed `WorkingMemorySnapshot` and `TraceCursorSnapshot` fields.
- Branch isolation is snapshot/restore based; evaluation and search may rely on branch publications and branch resume snapshots as durable records.
- `openai_trace.py` has a session-scoped canonical call store, grouped rebuild APIs, materialization manifests, and grouped session/build/solve/runtime-task views.
- `agintor.runner.TaskRuntime` is only the public facade. Runtime implementation lives under `agintor/task_runtime/`, and evaluation must treat it as bundled runtime-kernel internals rather than a factory import target.

## Boundaries

- Own benchmark content, typed task adapters, verifier catalogs, staged evaluation, robustness measurement, contamination control, search policy, search-state persistence, validation reporting, and held-out reporting.
- Own goal-conditioned family scoring, goal-scoped task selection, bounded local template synthesis, and benchmark provenance artifacts.
- Consume Workstream 3 run-state, checkpoint-lineage, recovery, memory-lineage, and grouped-trace query APIs. Do not create a second durability authority for evaluation.
- Keep runtime-state semantics, orchestration mechanics, tool sandbox internals, provider transport behavior, trace-store topology, and solve-time policy implementation outside this workstream.
- Keep open-ended benchmark invention, free-form grader generation, and internet-dependent benchmark lanes outside the MVP lane.

## Legacy Filters

Carry forward only the legacy gap items that match the current repository and this workstream's ownership.

- Do carry forward the goal-family selection gap: `agintor/goal_rubric.py` still caps selection with `max_families=2`, and `agintor/runtime_builder.py` still uses `goal_conditioned_demo_clone`.
- Do carry forward benchmark-provenance work: `planning/benchmark_provenance.json` is still missing as a first-class artifact.
- Do carry forward serious benchmark pressure: repo-patch and service-action execution nodes exist, but typed benchmark adapters, fixtures, provenance, and verifier evidence do not yet make them serious proof lanes.
- Do carry forward search-signal sufficiency: archive, scheduler, and predictor machinery exists, but fully evaluated evidence is still too thin and search state is not resumable as a process.
- Do not carry forward trace-topology work from `TRACE_AND_PLANNING_IMPROVEMENTS_PLAN.md`; Workstream 3 owns session-scoped raw-call storage, grouped materialization, and rebuild surfaces.
- Do not carry forward old "first-class checkpoint/resume missing" claims into this workstream; after WS3, checkpoint and recovery state are inherited inputs.
- Do not carry forward browser or multimodal claims as MVP proof lanes. `browser_task` remains adapter scaffold; `multimodal_task` remains placeholder.
- Do not introduce free-form provider-authored graders, internet benchmark lanes, or open-ended benchmark invention.
- Do not bypass the runtime host or import `agintor/task_runtime` internals for evaluation shortcuts.

## Planning Constraints

- `BenchmarkPlan` and `VerifierBundle` are the only legal evaluation inputs once frozen.
- `BenchmarkPlan` is a goal-scoped subset selected from the benchmark library and approved local templates, not a hidden alias for the full demo suite.
- Later stages must not silently reparse the original goal or rebuild suites from scratch.
- A frozen plan resolves to a concrete task registry. `evaluator.py`, `evolution.py`, and `archive.py` must not use raw `suite.train`, `suite.proxy`, `suite.val`, or `suite.test` as the authority for objective generation, Stage 1-4 task selection, validation, full-train batching, or held-out reports.
- Suite objects may remain the in-memory container, but every task used by evaluation must be selected through a plan-scoped partition entry that carries its task ID, adapter kind, fixture refs, verifier refs, contamination flags, and provenance.
- Fixture setup and environment digests must be frozen separately from task selection.
- Provider assistance may propose revisions only inside the known task library and approved template set. The local deterministic planner remains the final owner of task selection, verifier freeze, and artifact finalization.
- MVP proof lanes stay bounded:
  - first serious lane: `repo_patch`
  - second serious lane: `service_task`
  - `browser_task` lands as scaffold, not the first gating lane
  - `multimodal_task` stays placeholder-only until the rest of the pipeline is stable

## Baseline

- `agintor/benchmarks.py` already supports suites with `train`, `proxy`, `val`, and `test` partitions.
- `agintor/verifiers.py` already supports exact and near-exact local verification plus the `local`, `subtree`, `repo`, and `benchmark` checker ladder.
- `agintor/evaluator.py` already has the right staged-evaluation skeleton: patch integrity, deterministic smoke, proxy stage, local subset stage, and full-train stage.
- `agintor/archive.py` and `agintor/evolution.py` already implement objective islands, scope scheduling, staged acceptance, and predictor updates.
- `agintor/runtime_api.py` and `agintor/task_runtime/bounded_io.py` already support bounded `repo_patch` execution-plan nodes, and `service_action` node validation exists in `agintor/schemas.py`; these are execution primitives, not yet serious benchmark lanes.
- `OpenAITraceContext` already exists and flows through runtime request, plan, frame, branch, event, and receipt surfaces. Workstream 4 owns factory-side stamping for planning, mutation, patch repair, and search decisions.
- Runtime kernel bundling now includes `agintor/task_runtime/*.py`; Workstream 4 must keep evaluation routed through `RuntimeHost` and bundled runtime entrypoints.
- Goal-family selection still caps relevance too aggressively, and benchmark planning still collapses rich goals into too few tasks per family.
- `benchmark_provenance.json` is expected by the architecture but still lacks a concrete coverage and synthesis contract.
- Factory-side hosted planning and mutation calls are not yet stamped with stable build and search trace context even though the runtime-side trace contract exists.
- The current suite is still dominated by small structured tasks, and evaluation is stronger than the benchmark pressure it measures.
- Search state is still too process-local, and objective or operator policy is still too simplistic for the optimizer machinery already present.

Existing verifier and reporting surfaces are not greenfield work. `VerifierSpec` and `VerifierBundle` already exist, evaluation already runs through `RuntimeHost`, and `evolution.py` / `runtime_builder.py` already write validation history, stage failures, archive indexes, and leaderboards. Workstream 4 extends those surfaces into plan-scoped, fixture-aware, held-out-reportable artifacts.

## Reference Benchmark Ladder

- `structured_ops`: deterministic local computational and structured-output pressure
- `repo_patch`: repository editing, patch application, and local test execution under frozen fixtures
- `service_task`: bounded stateful tool or service workflows with deterministic fixture state transitions
- `browser_task`: scaffolded DOM or state assertions under frozen local fixtures
- `multimodal_task`: placeholder only until the rest of the runtime and evaluator stack are stable

## Core Decisions

- Treat frozen `BenchmarkPlan` and `VerifierBundle` as the only legal evaluation inputs.
- Make `BenchmarkPlan` a goal-scoped subset frozen from the benchmark library and approved local templates rather than carrying the full demo train set by default.
- Measure runtimes through the runtime entrypoint, not through hidden direct imports.
- Keep benchmark growth bounded and locally judgeable.
- Make repo editing the first serious proof lane.
- Make stateful service tasks the second serious proof lane.
- Build `browser_task` support in the adapter registry, but do not make browser flows an MVP gating lane before repo and service tasks are stable.
- Keep `multimodal_task` as a later adapter slot, not an MVP proof target.
- Add a simplification or refactoring operator to the search portfolio so the system can improve by getting smaller and cleaner, not only by getting more elaborate.
- Fix the signal bottleneck explicitly. Search policy must preserve enough full-train and held-out evidence to justify archive, scheduler, and predictor complexity.
- Use this exact goal-family rule:
  - compute all family scores
  - select every family with score `>= 2`
  - if none meet that threshold, select the top 2 positive-scoring families
  - if no family has a positive score, default to `["e2e", "top"]`
  - force-include `e2e` when the goal implies export, deployment, verification, orchestration, workflow completion, or composite reports
- Require at least 2 train tasks per selected family when available and at least 1 proxy task per selected family when available.
- Trigger bounded synthetic task generation only when deterministic coverage remains weak after selection.
- Name the planning strategy `goal_scoped_multi_select_v1`.
- Explicitly deprecate the current `goal_conditioned_demo_clone` path. `build_goal_conditioned_suite()` may survive only as a temporary migration shim for tests and demo compatibility; the authoritative builder path must use `goal_scoped_multi_select_v1`, multi-task partition entries, benchmark provenance, and a plan-scoped task registry.

## Schema Upgrades

Add the minimum typed schema surface needed to make the frozen benchmark plan authoritative without collapsing everything into `BenchmarkTask.metadata`.

- Add `BenchmarkPartitionEntry` or equivalent with:
  - `task_id`
  - `partition`
  - `family` (`top`, `mem`, `tool`, or `e2e`)
  - `adapter_kind` (`structured_ops`, `repo_patch`, `service_task`, `browser_task`, or `multimodal_task`)
  - `fixture_ids`
  - `environment_digest`
  - `verifier_ids`
  - `contamination_flags`
  - `source_task_id`
  - `template_id`
  - `goal_criteria_targets`
  - `transform_summary`
  - `selection_kind`
- Add a `BenchmarkProvenance` schema for `planning/benchmark_provenance.json` rather than leaving it as an untyped ad-hoc dictionary.
- Keep compatibility task-ID lists on `BenchmarkPlan` only as projections while callers are migrated. The canonical evaluation surface is the partition-entry set.
- Add summary-path fields so new artifacts are visible from CLI results:
  - `BuildSummary.benchmark_provenance_path`
  - `BuildSummary.held_out_report_path`
  - `BuildSummary.search_state_path`
  - `BuildSummary.signal_sufficiency_path`
  - matching `EvolutionSummary` fields where the artifact is produced by search
- `repo_patch` and `service_task` are adapter kinds or task types mapped into the existing benchmark families. Do not add new peer benchmark families for them.

## Phase 1: Make Frozen Planning Artifacts Authoritative

- Make `BenchmarkPlan` and `VerifierBundle` the only legal inputs to:
  - `runtime_builder.py`
  - `evaluator.py`
  - `evolution.py`
- Remove hidden suite reconstruction from later stages.
- Route all evaluation through `RuntimeHost` and the bundled runtime execution entrypoint. Do not call `agintor/task_runtime` implementation modules directly from factory evaluation.
- Reload `BenchmarkPlan`, `VerifierBundle`, `benchmark_suite.json`, and `benchmark_provenance.json` from disk at evaluation and resume boundaries so the frozen artifacts, not live Python objects, are authoritative.
- Build a plan-resolved task registry from `BenchmarkPlan` partition entries plus `benchmark_suite.json`. All evaluator partition access, objective construction, proxy selection, Stage 1 smoke task choice, Stage 2 touched-scope proxy choice, Stage 3 local subset construction, Stage 4 full-train batches, validation, and held-out reporting must read through this registry.
- Update `objective_specs_from_suite(...)` or replace it with a plan-scoped objective builder so single-task, family, robustness, and global objectives are computed only over the frozen selected task set.
- Update current normalization tests around `build_goal_conditioned_suite()` and `_normalize_benchmark_plan_against_suite()` into compatibility tests for the migration shim, then add new tests for `goal_scoped_multi_select_v1` task registry resolution.
- Use Workstream 3 `RunStore` and `state_store.py` query APIs to attach evaluation runs to checkpoint lineage, recovery outcomes, receipt lineage, grouped trace refs, and artifact lineage.
- Treat paused or resumed outcomes as first-class evaluation states. A resumable checkpoint, recovery attempt, or fail-closed recovery outcome must be visible in stage-failure and validation ledgers rather than flattened into an anonymous exception.
- Extend the existing persisted reporting surfaces and add the missing artifacts:
  - `validation_history.json`
  - `stage_failures.json`
  - `leaderboard.json`
  - `evolution_history.json`
  - `archive_index.json`
  - `search_state.json`
  - `search_resume_manifest.json`
  - `signal_sufficiency.json`
  - held-out report artifacts
  - `benchmark_provenance.json`
- Persist `planning/benchmark_provenance.json` as a first-class frozen artifact beside `BenchmarkPlan` and `VerifierBundle`.
- `benchmark_provenance.json` must record at least:
  - planning strategy
  - family scores
  - selected families
  - partition task selection
  - required capabilities
  - capability coverage
  - success-criteria coverage
  - source-task and template provenance
  - synthesis decision
  - provider-assist decision and applied revisions
- Treat `benchmark_provenance.json` as a sibling in the current planning chain with `AssumptionRegister`, `PlanningDiagnostics`, and `ReplanContract`; preserve `raw_goal_reparse_allowed=False`.
- Stamp factory-side hosted planning, mutation, and patch-repair calls with the existing `OpenAITraceContext`, including `provider_role="factory"` and available fields such as `session_id`, `build_id`, `iteration`, `objective`, `touched_scope`, `runtime_hash`, and `runtime_dir`. Required call sites include `_maybe_provider_refine_planning(...)`, `ProviderPatchMutator`, patch repair, objective choice, and operator choice.
- Record enough provenance to rerun evaluation from disk without reopening the raw goal prompt or implicitly rebuilding the suite.
- Do not emit validation or test trace refs into mutation prompts. Use WS3 grouped trace references only for reporting, diagnostics, and held-out evidence.

## Phase 2: Build a Typed Benchmark Adapter Registry

- Add a typed adapter registry in `agintor/benchmarks.py` for at least:
  - `structured_ops`
  - `repo_patch`
  - `service_task`
  - `browser_task`
  - `multimodal_task`
- Keep current structured tasks as `structured_ops`; do not rename the existing family taxonomy (`top`, `mem`, `tool`, `e2e`) into adapter names.
- Promote `repo_patch` from solve-request execution primitive to serious benchmark lane by adding frozen repository fixtures, declared writable targets, deterministic patch application rules, local test commands, environment digests, and patch-shape verifier refs.
- Promote `service_task` only through local deterministic fixtures with explicit initial state, allowed transitions, transport policy, and final-state verifier refs. Do not require internet services.
- Add `browser_task` adapter schema only after repo and service fixture contracts are stable; browser tests are scaffold coverage, not MVP proof claims.
- Keep `multimodal_task` as a schema placeholder only.
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
- Change selection defaults to:
  - at least 2 train tasks per selected family when available
  - at least 1 proxy task per selected family when available
  - preservation of cross-family `e2e` pressure whenever the goal implies orchestration, verification, export, workflow completion, or composite reports
- Trigger bounded synthetic generation if any remain true after deterministic selection:
  - a required capability is uncovered
  - a required success criterion is uncovered
  - a selected family has fewer than 2 train tasks when the library has 2 or more
  - a selected family has no proxy task when a proxy exists for that family
- Approved local template IDs:
  - `top.multi_op_structured_v1`
  - `top.checkpoint_trace_variant_v1`
  - `mem.exact_symbol_compaction_v1`
  - `mem.exact_path_resume_v1`
  - `tool.underspecified_expression_v1`
  - `tool.reuse_vs_create_variant_v1`
  - `e2e.composite_numeric_report_v1`
  - `e2e.composite_memory_tool_v1`
- Every selected, cloned, or synthesized task must record:
  - `source_task_id`
  - `template_id`
  - `goal_criteria_targets`
  - `transform_summary`
  - `verifier_origin`
- Template transforms may:
  - change prompt wording
  - change literals, symbol names, row values, context volume, and dependency annotations
  - omit an explicit expression for under-specified generated-expression tasks
- Template transforms may not:
  - change task family
  - change verifier class
  - change artifact shape or output keys
  - introduce external side effects
  - broaden allowed tool categories beyond the source task family
- Provider-assisted planning may propose revised task IDs and approved template IDs only within the known benchmark library and approved local template set. The local deterministic planner remains the final owner of final selection and verifier freeze.

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
- Link verifier evidence to Workstream 3 artifact refs, receipt refs, runtime-event refs, and grouped trace refs when those records support a score. The verifier evidence remains factory-owned, but its supporting runtime lineage comes from WS3.
- Keep validation and test verifier outputs mutation-invisible.

## Phase 4: Harden Evaluation, Held-Out Policy, and Contamination Control

- Add family-specific held-out rules:
  - fixed train, validation, and test for deterministic local tasks
  - freshness-controlled held-out lanes for repo tasks where feasible
  - environment fingerprints for service and browser tasks
  - contamination flags for cloned or synthesized descendants
- Use Workstream 3 `EnvironmentFingerprint`, `RecoveryAttempt`, `WorkingMemorySnapshot`, and grouped trace refs as reporting inputs for held-out reproducibility and recovery analysis.
- Add contamination records for:
  - selected source tasks reused across train, validation, and test
  - cloned descendants sharing a template or source fixture
  - synthetic descendants sharing verifier shape or fixture state
  - provider-assisted planning proposals accepted into the frozen plan
- Fail held-out claim generation if validation or test traces, verifier outputs, or grouped trace transcripts are present in mutation-prompt inputs.
- Persist stage-failure rows with:
  - stage
  - candidate hash
  - touched scope
  - operator type
  - benchmark refs
  - verifier refs
  - evaluation-unit ID
  - request ID
  - task ID
  - episode kind and step index where applicable
  - run ID, attempt ID, checkpoint ref, recovery attempt refs, and grouped trace refs when available
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
- Persist search state as canonical JSON under `evolution/` with optional WS3 index references. Do not store the only copy of archive, scheduler, or predictor state in SQLite.
- Add `search_state.json` and `search_resume_manifest.json` so `evolve` and `build-runtime` can resume from a known iteration, RNG state, archive snapshot, scheduler phase, and benchmark/verifier artifact hash.
- `search_state.json` must include at least:
  - current iteration
  - archive cells and runtime directories
  - scheduler phase, scope credits, counterfactual credits, hard-failure rates, staleness, and need scores
  - objective history and current objective selector state
  - predictor snapshot refs and retraining counters
  - phase budgets and consumed budgets
  - stage pass-rate counters and tightened thresholds
  - accepted and rejected lineage
  - operator portfolio state
  - RNG state
  - current leader set and validation cursor
- `search_resume_manifest.json` must include digests for `BenchmarkPlan`, `VerifierBundle`, `benchmark_provenance.json`, `benchmark_suite.json`, baseline runtime identity, runtime ABI, kernel version, storage schema version, and runtime profile inputs.
- When resuming, validate that the loaded search state points to the same frozen `BenchmarkPlan`, `VerifierBundle`, `benchmark_provenance.json`, runtime ABI, kernel version, and storage schema version. Fail closed on mismatch.
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
- Provider-assisted mutation and patch repair must receive factory `OpenAITraceContext` values. Context construction belongs in the factory call sites; provider adapters only preserve it.

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
- Add an explicit `signal_sufficiency.json` report that records whether current Stage 4 volume can support archive insertion, scheduler credit, and predictor retraining. This report must be consumed by Workstream 5 before predictor-backed solve-time decisions are expanded.

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
- Keep `browser_task` implemented as a scaffolded adapter, but do not base MVP claims on it.
- Require every exported leader claim to cite:
  - benchmark plan ID
  - verifier bundle ID
  - benchmark provenance ID
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

## Regression Coverage

- Add tests proving:
  - evaluation reruns from frozen disk artifacts
  - evaluation attaches run results to WS3 checkpoint lineage, recovery records, receipt refs, and grouped trace refs without creating a second state authority
  - typed adapters resolve fixtures deterministically
  - verifier evidence bundles replay cleanly
  - contamination flags are preserved
  - validation and test traces never enter mutation context
  - search state resumes consistently
  - `search_resume_manifest.json` fails closed when benchmark, verifier, runtime ABI, kernel version, or storage schema digests mismatch
  - operator choice and lineage are logged
  - leader export references the correct held-out report
  - `benchmark_provenance.json` is written, reloaded, and used by evaluation instead of regenerated from raw goal text
- Keep all default evaluation tests offline and deterministic.

## Handoff to Workstream 5

- Workstream 5 receives:
  - typed benchmark families
  - typed verifier evidence
  - benchmark provenance and contamination records
  - durable evaluation ledgers
  - contamination controls
  - resumable search state
  - serious held-out reports
  - signal-sufficiency reports
  - predictor snapshots and search-side summaries safe for runtime consumption
  - operator and objective-choice ledgers
- Workstream 5 must use that evidence to decide which tooling, provider, and control decisions deserve runtime-owned contracts and predictor-backed policies.
- Workstream 5 must not redesign benchmark planning, verifier semantics, archive accounting, grouped trace topology, or search-state persistence.

## Acceptance Criteria

1. `build-runtime` and `evolution` consume frozen `BenchmarkPlan` and `VerifierBundle` artifacts rather than reconstructing suites implicitly.
2. Evaluation runs through the runtime entrypoint rather than hidden direct-import execution paths.
3. The workspace contains durable evaluation artifacts, including validation history, stage failures, leaderboard snapshots, and benchmark provenance.
4. At least one serious non-demo benchmark family is active with typed fixtures and typed local verifiers.
5. Search can resume from persisted archive and scheduler state without rebuilding the world from scratch.
6. Search reporting exposes whether the optimizer has enough full-train evidence to justify its current complexity.
7. An exported leader is backed by a reproducible held-out report on a serious suite, not only by structured-demo improvements.
8. Rich goals select more than two relevant families when justified, and benchmark plans freeze only the selected goal-scoped subset rather than the full demo train set by default.
9. Selected families carry multi-task train pressure and at least one proxy task when the benchmark library provides them.
10. Any cloned or synthesized task is bounded to approved local templates and carries explicit source-task, transform, criteria-target, and verifier provenance.
11. Provider-assisted planning cannot move task selection outside the known benchmark library and approved local template set.
12. Evaluation and search reports link to WS3 run-state lineage, checkpoint lineage, recovery records, receipt refs, and grouped traces without copying or redefining those stores.
13. `signal_sufficiency.json` or equivalent reporting is present before Workstream 5 expands predictor-backed solve-time control.
14. `BuildSummary` and `EvolutionSummary` expose paths to benchmark provenance, search state, signal sufficiency, and held-out report artifacts.
15. Objective generation, stage task selection, validation, and held-out reporting are plan-scoped and cannot accidentally fall back to the full demo suite.

## File Ownership

- `agintor/benchmarks.py`: adapter registry, fixture references, benchmark provenance
- `agintor/schemas.py`: benchmark partition entries, benchmark provenance schema, fixture refs, contamination records, held-out report records, and summary-path fields on build/search summaries
- `agintor/goal_rubric.py`: family scoring, family-selection thresholds, and goal-conditioned coverage inputs
- `agintor/verifiers.py`: typed verifier catalog and evidence serialization
- `agintor/evaluator.py`: staged evaluation, plan-scoped task registry consumption, failure ledgers, validation history, held-out policy, and runtime-host-only execution routing
- `agintor/archive.py`: archive descriptors, persisted archive state, and plan-scoped objective construction
- `agintor/evolution.py`: objective selection, operator portfolio, resume support, search-state persistence, signal-sufficiency reports, leader tracking, and factory trace context for mutation calls
- `agintor/runtime_builder.py`: goal-scoped benchmark planning, deterministic coverage pass, and benchmark provenance artifact emission
- `agintor/mutator.py`: simplification and refactoring operator hooks
- `agintor/crossover.py`: bounded whole-method crossover
- `agintor/predictors.py`: predictor snapshot persistence and search-side summaries
- `agintor/run_store.py` and `agintor/state_store.py`: consumed by Workstream 4 as WS3 durability/query dependencies only; do not move evaluation ownership into them
- `agintor/openai_trace.py`: consumed for grouped trace refs and factory call stamping only; trace topology remains WS3-owned
- `agintor/runner.py` and `agintor/task_runtime/`: runtime host internals consumed only through `RuntimeHost` and bundled runtime entrypoints; Workstream 4 must not add benchmark or search logic there
- `tests/test_runtime_builder.py`, `tests/test_evolution.py`, `tests/test_benchmarks_plugin.py`, and adjacent new tests: benchmark, verifier, contamination, and search-resume regression coverage

## Deferred

- Open internet benchmark lanes
- Free-form grader generation
- Cloud-first distributed search as the primary lane
- Multimodal proof campaigns before repo and service lanes are stable
