# Workstream 4 Replacement: Bounded Progress Evaluation

## Status

This document is the WS4 replacement plan for implementing `Agintor_Bounded_Progress_Evaluation.md`.

The original `implementation_workstreams/WORKSTREAM_4_BENCHMARKS_EVALUATION_AND_SEARCH.md` stays intact. Its useful pieces remain valid where they support this plan: runtime-host-only evaluation, frozen evaluation artifacts, provenance, contamination control, durable search state, held-out reporting, and serious bounded proof lanes. Where the original benchmark-first plan conflicts with this document, this document wins.

## Outcome

Agintor must stop treating benchmark score deltas as capability progress by default. WS4 must instead build bounded domain evidence institutions that can say, with explicit scope:

- this child is more capable on a grounded domain;
- this child is only more efficient at the same quality;
- this child only improves a subskill;
- this result is only preference evidence;
- this evidence is too weak, leaked, noisy, unrealistic, or statistically invalid, so Agintor abstains.

The load-bearing invariant is:

```text
No capability promotion without a Domain Evidence Contract.
```

## Product Boundary

Agintor is the factory. WS4 is factory-side evaluation and search infrastructure for deciding which generated runtime should be promoted inside a build/evolution project.

Built runtimes are evaluated through `RuntimeHost` and the runtime entrypoint. WS4 must not call runtime-kernel internals directly, must not invent runtime prompt categories, and must not expose hidden validation data to mutation prompts.

Benchmark tasks remain evaluation machinery. They are not the product surface and they are not the source of truth for capability claims. The source of truth is the domain evidence contract plus the evidence ledger produced under it.

## Current Baseline

The current implementation already has useful scaffolding:

- `agintor/evaluation/evaluator.py` runs staged parent/child evaluation through `RuntimeHost`.
- `agintor/evaluation/scoring.py` computes utility from verifier score minus cost, latency, and fault penalties.
- `agintor/evaluation/verifiers.py` supports exact, numeric, string, and trace-event checks.
- `agintor/evaluation/benchmarks.py` defines demo `structured_ops`, `memory_query`, `tool_expression`, and `e2e_report` tasks.
- `agintor/search/engine.py` mutates runtimes, evaluates children, inserts Stage 4 survivors into the archive, updates scheduler credit, and trains predictors.
- `agintor/search/archive.py` maintains quality-diversity cells over objective scores and behavior descriptors.
- `agintor/learning/observations.py` labels predictor observations from run-level success, latency, token, and fault data.
- `agintor/contracts/factory.py`, `agintor/contracts/benchmarks.py`, and `agintor/contracts/search.py` hold the current public schema surface.

The hard gap is not mechanics. The hard gap is epistemic authority. The current loop can reward `verifier_score - cost - latency - faults`, but it cannot yet prove that a reward delta means capability improvement rather than cheaper execution, trace-shape luck, saturated exact tasks, generated-task junk, verifier weakness, leakage, or optional-stopping noise.

## Non-Goals

- No open-ended oracle claims.
- No free-form provider-authored graders in the promotion path.
- No LLM-judge-only capability promotion.
- No internet-dependent benchmark lane for the MVP.
- No broad repo-patch proof lane before the first exact-answer evidence institution works.
- No backward compatibility for disposable MVP evaluation artifacts.

## Core Architecture

WS4 introduces one new conceptual object above the existing `BenchmarkPlan` and `VerifierBundle`:

```text
DomainEvidenceContract
```

A domain evidence contract freezes the evaluation world for a bounded domain. It defines what tasks may be sampled, how private answers are known, what quality axes mean, how generator and validator health are measured, how leakage is prevented, what statistical rule governs promotion, and how far the resulting claim may generalize.

`BenchmarkPlan` and `VerifierBundle` should become compatibility projections from this contract, not the authority themselves.

The factory-side signal path becomes:

```text
Goal
  -> DomainEvidenceContract
  -> ChallengeGeneratorVersion
  -> ChallengeInstance set
  -> sealed parent/child runtime runs through RuntimeHost
  -> EvidenceLedger records
  -> PairedComparison records
  -> PromotionDecision
  -> scoped optimizer update
```

There is no useful training signal "out of thin air." Signal comes from hidden, independently checkable consequences inside bounded domains.

## Required Schemas

Add a new contract surface, preferably `agintor/contracts/evidence.py`, and export it from `agintor/contracts/__init__.py`.

### `DomainEvidenceContract`

Fields:

- `contract_id`
- `domain_kind`: `generated_tool_workflow`, `structured_memory_retrieval`, `stateful_service`, `repo_patch`, `preference`
- `version`
- `scope`: domain name, slice tags, allowed claim language
- `challenge_distribution`: generator refs, slice weights, difficulty ranges, partition policy
- `answer_mechanism`: exact interpreter, graph query engine, state simulator, acceptance predicate, human/user feedback, or capped judge proxy
- `quality_axes`: axis specs with authority type, threshold `epsilon`, protected-regression tolerance, and promotion eligibility
- `efficiency_axes`: token, walltime, provider cost, tool calls, retry count
- `health_floors`: generator, answer, validator, realism, statistics, leakage
- `statistical_rule`: fixed confirmatory budget or anytime-valid confidence sequence
- `leakage_policy`: hidden data isolation, template lifecycle, mutation feedback restrictions
- `feedback_policy`: what may be sent to mutators after failed or partial evaluations
- `artifact_refs`: frozen source files, fixture refs, generator versions, verifier versions

### `ChallengeGeneratorVersion`

Fields:

- `generator_id`
- `domain_kind`
- `version`
- `template_state`: `design`, `train`, `validation`, `confirmatory`, `retired`
- `difficulty_parameters`
- `slice_coverage`
- `private_answer_available`
- `health_report_ref`
- `realism_report_ref`
- `retirement_reason`

Generator versions are scientific instruments. They do not gain authority by being generated by an agent.

### `ChallengeInstance`

Fields:

- `challenge_id`
- `contract_id`
- `generator_id`
- `partition`: `explore`, `train`, `validation`, `confirmatory`, `heldout`
- `domain_kind`
- `slice_tags`
- `difficulty_vector`
- `public_prompt`
- `public_fixture_refs`
- `private_answer_ref`
- `metamorphic_relation_refs`
- `validator_refs`
- `contamination_flags`
- `template_lineage`

The private answer must never be embedded in the public prompt, mutation prompt, trace prompt, or runtime artifact.

### `EvidenceRecord`

Fields:

- `record_id`
- `contract_id`
- `challenge_id`
- `candidate_runtime_hash`
- `parent_runtime_hash`
- `run_ref`
- `attempt_ref`
- `checkpoint_refs`
- `trace_refs`
- `artifact_ref`
- `axis_scores`
- `efficiency_scores`
- `verifier_evidence`
- `defect_evidence`
- `metamorphic_evidence`
- `authority_level`
- `invalid_reason`

This becomes the atom of the evidence ledger. Existing `RunResult` remains runtime output; `EvidenceRecord` is the factory-side interpretation of that output under a frozen contract.

### `PairedComparison`

Fields:

- `comparison_id`
- `parent_runtime_hash`
- `child_runtime_hash`
- `contract_id`
- `challenge_ids`
- `axis_deltas`
- `protected_axis_bounds`
- `efficiency_deltas`
- `confidence_intervals`
- `alpha_spent`
- `health_floor_status`
- `leakage_status`
- `decision_ref`

Every capability claim must be parent/child paired on the same sealed challenge set or on a statistically valid adaptive policy fixed before observing each paired result.

### `PromotionDecision`

Fields:

- `decision_id`
- `decision_type`: `capability`, `efficiency`, `preference`, `subskill`, `abstain`, `reject`
- `contract_id`
- `scope`
- `winning_runtime_hash`
- `parent_runtime_hash`
- `comparison_ref`
- `allowed_optimizer_updates`
- `forbidden_optimizer_updates`
- `reason_codes`
- `alpha_spent`
- `evidence_refs`

`abstain` is a first-class result. It means the evaluation world cannot honestly distinguish good from better for the claim being tested.

## Promotion Semantics

Capability promotion is legal only when all of these hold:

- at least one promotion-eligible capability axis has lower confidence bound above its minimum meaningful improvement;
- all protected axes avoid unacceptable regression;
- generator, answer, validator, realism, statistics, and leakage health pass their floors;
- private answers and confirmatory templates were not exposed to the mutator;
- the statistical receipt is valid under fixed-budget or anytime-valid rules;
- the claim scope is explicit and bounded.

Efficiency promotion is legal when grounded quality is equivalent within tolerance and cost, latency, token, or tool-call use improves. Efficiency must not update capability priors.

Preference promotion requires human/user-grounded preference labels or observable downstream outcomes. LLM judges may triage, generate defect hypotheses, or bootstrap preference models, but judge-only evidence cannot certify capability.

Subskill promotion is legal when a child improves a grounded subclaim but still fails the full release criterion. This is useful for evolution and must be kept distinct from deployable leader promotion.

Reject means the child is worse or invalid under the contract. Abstain means the contract or evidence is insufficient.

## Scoring Changes

Replace the current single utility function as the promotion authority.

`agintor/evaluation/scoring.py` should split scoring into:

- `QualityVectorScorer`: computes grounded axis scores from evidence records.
- `EfficiencyScorer`: computes cost, latency, token, tool-call, and retry deltas.
- `PairedEffectEstimator`: computes paired deltas and confidence intervals.
- `PromotionRule`: maps paired effects plus health receipts into a `PromotionDecision`.

The current formula:

```text
verifier_score - cost_penalty - latency_penalty - fault_penalty
```

may remain as an exploration or ranking heuristic, but not as the authority for capability promotion.

`mean_improvement()` must be replaced or wrapped by a paired statistical routine that records whether the estimate is exploratory, fixed confirmatory, or anytime-valid. Naive "sample until the child looks better" is illegal for promotion.

## Evaluation Flow

The staged evaluator should be re-centered around contracts.

### Stage 0: Candidate Integrity

Keep the current patch integrity, mutable-boundary, syntax, and runtime-load checks. These are validity gates, not progress evidence.

### Stage 1: Contract and Instrument Health

Load the frozen `DomainEvidenceContract`, generator versions, challenge instances, answer mechanism, validators, and health reports from disk. Fail closed if any digest mismatches.

For generated domains, run generator-health checks before candidate evaluation:

- positive controls pass;
- known bad solvers fail;
- baseline ladder monotonicity holds;
- private answer derivation is stable;
- challenge tasks are not duplicated across train/confirm partitions;
- no answer leakage is detected.

### Stage 2: Explore

Run exploratory probes for diagnosis, frontier estimation, weak-axis discovery, and mutator feedback. This stage may update exploration statistics and prompt future mutations with safe slice-level feedback.

It may not promote.

### Stage 3: Lock

Freeze the confirmatory evaluation:

- challenge IDs or challenge-sampling rule;
- parent and child runtime hashes;
- quality axes and protected axes;
- thresholds;
- alpha budget;
- generator and validator versions;
- leakage policy;
- feedback restrictions.

After lock, no task selection can depend on observed child advantage.

### Stage 4: Confirm

Run parent and child against the locked challenge set through `RuntimeHost`. Convert `RunResult` rows into `EvidenceRecord` rows. Compute paired comparisons and issue exactly one `PromotionDecision`.

The evaluator returns the decision, not only a `SuiteEvaluation`.

### Stage 5: Held-Out Report

For leaders and exported runtime claims, produce a held-out report that cites:

- contract ID;
- generator versions;
- challenge instance digests;
- private answer authority;
- validator health;
- realism health if the claim exceeds generated-domain scope;
- statistical receipt;
- promotion decision;
- run/checkpoint/trace refs from WS3-owned stores.

## First Evidence Institution: Generated Tool Workflow

The first MVP lane is `generated_tool_workflow_v1`, not repo patching.

Reason: the private answer can be computed by an independently audited typed DSL interpreter. This gives Agintor a strong early capability gradient without pretending open-ended software quality is already solved.

### Domain Shape

Generate typed tool-workflow tasks from expressions over private environments:

```text
value = interpret(expression, private_environment)
```

Public tasks expose only the prompt, available operation descriptions, declared input values, and expected artifact shape. Private answers live in sealed refs.

Difficulty is controlled by:

- expression depth;
- dependency width;
- type mix;
- distractor count;
- numeric edge cases;
- multi-step dependency chains;
- required memory lookup plus tool computation;
- hidden metamorphic variants.

### Answer Authority

The answer is computed by a deterministic interpreter, not by an LLM judge and not by the candidate runtime. The interpreter must be tested independently with golden fixtures, metamorphic checks, and bad-solver mutation tests.

### Quality Axes

Initial axes:

- `answer_exact`: exact or numeric-equivalent final answer;
- `dependency_correctness`: correct use of prior operation outputs;
- `type_robustness`: correct behavior across int, float, string, list, table, and null-like values where allowed;
- `distractor_resistance`: ignores irrelevant context and unused operations;
- `metamorphic_consistency`: preserves answers under equivalent expression rewrites;
- `tool_grounding`: uses allowed operations without forbidden shortcuts when the contract requires tool use.

Only grounded axes can support capability promotion.

### Anti-Saturation Rule

If parent and child both solve all exact tasks and no harder frontier, metamorphic, distractor, or hidden-slice evidence is available, the correct decision is:

```text
NoCapabilitySignal
```

Lower cost in that case can produce efficiency promotion only.

## Later Evidence Institutions

### Structured Memory Retrieval

Use private typed knowledge graphs and generated queries. Answers are graph denotations. Metamorphic checks include irrelevant-node insertion, graph isomorphism, stale-vs-fresh conflict rules, distractor removal, and provenance preservation.

This should become the second lane because it directly pressures Agintor's memory and retrieval policies with strong private answers.

### Stateful Service Tasks

Use local deterministic state machines before external services. The answer is a valid trace set under a transition model and invariant set. Quality axes include final-state correctness, invariant preservation, idempotence, replay behavior, recovery behavior, and minimal irreversible side effects.

This reuses existing `service_action` runtime primitives, but WS4 must wrap them in domain fixtures, transition simulators, and evidence contracts before claiming capability progress.

### Repository Patch Tasks

Repo patching is high value and harder. It requires acceptance predicates, hidden tests, mutation tests, fuzzing, diff-shape constraints, fixture digests, environment stability checks, and human/repo realism audits.

Carry forward the original WS4 repo-patch ambition, but move it after exact tool and memory institutions. Otherwise WS4 will again pretend that tests alone are a full oracle.

### Preference Tasks

Preference tasks are useful for product taste and open-ended usefulness. They are not core capability evidence unless grounded by human labels, real user outcomes, or independently checkable subclaims.

LLM judges are capped weak evidence. Many judge votes do not become high-authority evidence by accumulation.

## Generator Health Program

Add generator-health machinery before generated tasks can promote candidates.

A generator health report must measure:

- syntactic validity;
- answer reliability;
- solvability by positive controls;
- rejection of known bad solvers;
- baseline ladder monotonicity;
- task nontriviality;
- template diversity;
- anti-leakage;
- metamorphic coherence;
- calibration stability;
- realism transfer, when making real-domain claims.

Unhealthy generators are quarantined. A quarantined generator may still create diagnostic tasks, but its tasks cannot support capability promotion.

Generated instruments evolve in a separate instrument loop. Candidate runtimes evolve against frozen contracts. A generator cannot promote itself by making the current child look good.

## Search and Optimizer Update Rules

`agintor/search/engine.py` must stop inserting a child into the capability archive just because Stage 4 produced a non-invalid score.

The new rule:

```text
PromotionDecision controls optimizer updates.
```

Allowed updates:

- `capability`: insert into capability archive, update capability scheduler credit, update capability predictor labels.
- `subskill`: insert into subskill archive, update slice/subskill credit, do not export as deployable leader.
- `efficiency`: insert into efficiency archive, update cost/latency/token predictors only, do not change capability priors.
- `preference`: update preference archive/model only.
- `abstain`: record diagnostics and maybe schedule instrument improvement; do not update candidate capability.
- `reject`: record failure and update hard-failure/safety statistics.

`QualityDiversityArchive` should split into at least:

- `CapabilityArchive`
- `EfficiencyArchive`
- `SubskillArchive`
- optional `PreferenceArchive`

The current behavior descriptor can remain useful for diversity, but archive cell replacement must use decision-scoped quality axes, not blended utility.

`extract_predictor_observations()` must stop treating every accepted Stage 4 survivor as the same kind of success. Predictor labels need promotion type, domain scope, axis IDs, and evidence authority.

## Mutation Feedback Rules

Mutation prompts may receive:

- weak-axis names;
- public slice tags;
- public failure categories;
- public artifact-shape constraints;
- aggregate non-secret statistics;
- examples from train/explore partitions if the contract permits them.

Mutation prompts must not receive:

- private answers;
- confirmatory challenge contents;
- hidden templates;
- validation/test traces containing answer leakage;
- verifier evidence that reveals the hidden solution;
- generator internals marked private.

If leakage is detected, all affected evidence is invalidated and the relevant templates are retired.

## Persistence and Reporting Artifacts

Keep the original WS4 durability idea, but rename the center of gravity from benchmark provenance to evidence provenance.

Add or extend artifacts under the factory workspace:

- `planning/domain_evidence_contracts/*.json`
- `planning/challenge_generators/*.json`
- `planning/challenge_instances/*.jsonl`
- `planning/benchmark_provenance.json` as a compatibility summary
- `planning/verifier_bundle.json` as a compatibility/verifier projection
- `evaluation/evidence_ledger.jsonl`
- `evaluation/paired_comparisons.jsonl`
- `evaluation/promotion_ledger.jsonl`
- `evaluation/generator_health/*.json`
- `evaluation/validator_health/*.json`
- `evaluation/leakage_report.json`
- `evaluation/held_out_report.json`
- `evolution/capability_archive.json`
- `evolution/efficiency_archive.json`
- `evolution/subskill_archive.json`
- `evolution/search_state.json`
- `evolution/search_resume_manifest.json`
- `evolution/signal_sufficiency.json`

`BuildSummary` and `EvolutionSummary` should expose the important paths.

## Implementation Phases

### Phase 1: Contract Backbone

- Add evidence contract schemas.
- Add JSON persistence and digest validation.
- Extend build/evolution summaries with evidence artifact paths.
- Make evaluator fail closed when a capability promotion is requested without a domain evidence contract.
- Keep current demo benchmark evaluation available as diagnostic/compat mode only.

### Phase 2: Generated Tool Workflow Institution

- Implement the typed DSL or structured expression interpreter.
- Implement deterministic challenge generation with difficulty parameters.
- Implement private answer refs and no-leak public prompts.
- Implement tool-workflow validators that produce axis vectors.
- Add generator-health checks with positive controls and bad solvers.
- Add metamorphic variants for expression equivalence and distractor insertion.

### Phase 3: Paired Promotion Engine

- Add `EvidenceRecord`, `PairedComparison`, and `PromotionDecision` production.
- Replace capability gates based on raw score deltas with fixed confirmatory or anytime-valid paired rules.
- Split quality and efficiency scoring.
- Make `abstain` explicit and persisted.

### Phase 4: Search Integration

- Route archive insertion, scheduler credit, and predictor labels through `PromotionDecision`.
- Split capability, efficiency, and subskill archives.
- Persist search state with contract/generator/verifier digests.
- Fail closed on resume when contract or instrument digests mismatch.

### Phase 5: Anti-Junk and Leakage Hardening

- Add generator-health reports and template lifecycle states.
- Add leakage detection and template retirement.
- Add tests proving hidden answers and confirmatory examples cannot enter mutation prompts.
- Add signal-sufficiency reporting that distinguishes "not enough evidence" from "child is bad."

### Phase 6: Memory, Service, and Repo Lanes

- Add structured memory retrieval after tool workflow.
- Add stateful service tasks after memory.
- Add repo patch only once acceptance predicates, hidden tests, mutation tests, fuzzing, diff constraints, and realism audits exist.
- Keep browser and multimodal as scaffolds until grounded evidence contracts exist.

## Regression Coverage

Add focused tests proving:

- no `DomainEvidenceContract` means no capability promotion;
- cost-only wins produce efficiency promotion only;
- judge-only wins cannot produce capability promotion;
- saturated exact-answer tasks produce `NoCapabilitySignal` unless harder frontier evidence exists;
- private answers never appear in mutation prompts, traces, exported runtimes, or public challenge payloads;
- confirmatory task selection is locked before observing paired results;
- optional stopping without anytime-valid correction cannot promote;
- generator health failure quarantines the generator;
- baseline ladder monotonicity failure blocks generator authority;
- parent and child are evaluated on the same sealed challenge instances;
- `PromotionDecision` controls archive insertion;
- capability, efficiency, and subskill archives cannot update each other's priors;
- search resume fails closed on contract, generator, verifier, runtime, or profile digest mismatch;
- held-out reports cite evidence, health, leakage, and statistical receipts.

Keep default tests offline and deterministic.

## File Ownership

- `agintor/contracts/evidence.py`: new evidence contracts, challenge specs, evidence records, comparisons, promotion decisions, health reports.
- `agintor/contracts/benchmarks.py`: compatibility projections and existing task/verifier contracts; do not make this the capability authority.
- `agintor/contracts/factory.py`: summary-path fields for evidence artifacts.
- `agintor/contracts/search.py`: archive records and evolution rows must carry promotion decision refs and promotion types.
- `agintor/evaluation/benchmarks.py`: keep current suite loading; add or delegate domain-backed challenge resolution.
- `agintor/evaluation/verifiers.py`: typed validator execution and evidence output; no free-form provider graders.
- `agintor/evaluation/scoring.py`: quality vector, efficiency, paired effect, and promotion-rule scoring.
- `agintor/evaluation/evaluator.py`: contract loading, sealed parent/child evaluation, evidence ledger writing, promotion decisions.
- `agintor/evaluation/domains/`: new domain institutions for tool workflow, memory retrieval, service state, and repo patch.
- `agintor/search/engine.py`: consume promotion decisions for archive, scheduler, predictor, and cleanup decisions.
- `agintor/search/archive.py`: split archives or add promotion-typed archive stores.
- `agintor/learning/observations.py`: promotion-aware predictor observations.
- `agintor/learning/predictors.py`: separate capability, efficiency, and fault/latency families where needed.
- `agintor/factory/planning.py`: emit evidence contracts, generator specs, challenge instances, and compatibility benchmark/verifier artifacts.
- `agintor/factory/pipeline.py`: expose evidence artifact paths in build output.
- `agintor/runtime/host/`: consumed only through public host APIs.
- `agintor/runtime/kernel/`: no WS4 evaluation logic belongs here.
- `tests/`: add evidence-contract, tool-workflow, promotion, leakage, archive, and resume regression coverage.

## Handoff to WS5

WS5 receives scoped promotion evidence, not generic benchmark wins.

It may consume:

- capability promotion ledgers;
- efficiency promotion ledgers;
- subskill ledgers;
- predictor snapshots tagged by promotion type;
- evidence-contract scopes;
- held-out reports;
- signal-sufficiency reports.

WS5 must not reinterpret judge-only, cost-only, or abstained evidence as capability progress.

## Acceptance Criteria

1. A capability promotion cannot be created without a frozen `DomainEvidenceContract`.
2. The first implemented proof lane is `generated_tool_workflow_v1` with private interpreter-derived answers.
3. Parent and child candidates are compared on sealed paired challenge instances.
4. `ScoreCalculator` no longer acts as the capability-promotion authority.
5. `PromotionDecision` is the only input that can update capability archive state.
6. Efficiency, capability, preference, and subskill updates are separate.
7. Generator health, answer health, validator health, leakage status, and statistical receipts are persisted.
8. Mutation feedback cannot leak private answers or confirmatory examples.
9. Search resume validates evidence-contract and instrument digests.
10. Exact-task saturation produces abstention or efficiency-only progress, not fake capability progress.
11. Repo-patch and service-task lanes remain in scope, but only after their evidence contracts exist.
12. The exported leader claim cites the bounded scope and evidence ledger that justify it.

## Final Rule

Agintor becomes useful when it can honestly say:

```text
This runtime is better than its parent on this bounded domain,
under this frozen evidence contract,
on these grounded quality axes,
with these health checks,
with this statistical receipt,
and no broader claim is implied.
```

Anything less is diagnostic signal, preference signal, efficiency signal, or no signal. It is not capability progress.
