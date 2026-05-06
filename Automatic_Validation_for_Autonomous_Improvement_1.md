## Core answer

The solution is not “find the right metric.” It is to make validation a **typed evidence system**.

A run should update the optimizer only when the system can say:

> “This specific claim about the outcome is supported by this specific validator, whose authority, scope, leakage risk, calibration, and failure modes are known. The remaining parts are unverified.”

Call the architecture **CAVE: Claim–Authority Validation Engine**.

Its central rule:

> **Never train on a scalar reward unless the scalar is the projection of an evidence ledger with explicit authority, coverage, and uncertainty.**

This is necessary because optimizing against imperfect proxies creates reward hacking and Goodhart failures: policies can maximize the proxy while degrading the intended objective. Recent work on reward hacking frames this as a structural vulnerability of optimizing expressive policies against compressed proxy rewards rather than the latent objective itself. ([arXiv][1])

---

# 1. Validation authority taxonomy

Validation authority is **local to a claim**, not global to a task. A proof checker may strongly validate “the algorithm satisfies this invariant” while saying nothing about “the UX is good.”

|  Level | Authority class                              | What it can validate                                                                                                                         | Promotion rights                                                                    |
| -----: | -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| **A7** | Formal certificate                           | Machine-checkable proof, type proof, proof-carrying code, theorem-prover certificate, cryptographic proof                                    | Can certify the formal property, never the whole intent unless the spec is complete |
| **A6** | Direct executable oracle                     | Hidden tests from spec, exact expected output, simulator reward with known semantics, database postcondition, API read-after-write assertion | Strong promotion for covered claims                                                 |
| **A5** | Property / metamorphic / differential oracle | Invariants, randomized property tests, metamorphic relations, differential agreement between independent implementations                     | Strong negative evidence; moderate positive evidence unless coverage is measured    |
| **A4** | External factual authority                   | Official source, timestamped document, canonical dataset, independent citations                                                              | Strong for bounded factual claims; weak for interpretation                          |
| **A3** | Instrumented state/process evidence          | Logs, tool traces, state snapshots, provenance, access-control checks, side-effect boundaries                                                | Strong for “what happened,” weak for “was it valuable?”                             |
| **A2** | Calibrated learned evaluator                 | Model judge, reward model, preference model, classifier, verifier trained against trusted labels                                             | Weak-to-moderate only within calibrated domain                                      |
| **A1** | Heuristic critique                           | Self-critique, debate, consistency checks, style checks, reasoning traces                                                                    | Triage only; cannot promote alone                                                   |
| **A0** | Unverifiable                                 | Ambiguous, inaccessible, subjective, future-dependent, or underspecified claims                                                              | No winner; abstain or request new instrumentation                                   |

This taxonomy follows a known lesson from software testing: when no full oracle exists, partial oracles can still answer some questions, but many systems lack complete formal specs or assertions and therefore face an oracle problem. ([EECS Department][2])

---

# 2. What can be validated strongly, partially, or not at all?

## Strongly auto-validatable

These tasks have a validator whose semantics are close to the objective.

Examples:

* Code with precise specs, unit tests, property tests, hidden tests, type checks, performance bounds.
* Math or formal reasoning with proof certificates.
* Data transformations with known input/output invariants.
* Stateful workflows where success is a concrete postcondition: “calendar event exists with these fields,” “file was renamed,” “database row was updated once.”
* Deterministic parsing, extraction, formatting, schema conformance.
* Tool-use tasks in controlled environments with logged actions and read-after-write validation.

Proof-carrying code is the archetype of strong validation: the producer supplies a proof that code obeys a defined safety policy, and the host validates it with a proof checker. 

## Partially auto-validatable

These have necessary conditions but not sufficient conditions.

Examples:

* Software with incomplete tests.
* Factual answers where sources exist but may be stale, conflicting, or incomplete.
* Summaries, literature reviews, analyses, and explanations.
* Long workflows where final state can be checked but user satisfaction cannot.
* Open-ended design, strategy, writing, or planning tasks.
* Creative or semantic tasks with rubrics.

Metamorphic testing is useful here because it validates relationships between multiple executions rather than requiring a full oracle. It can alleviate the oracle problem, but its relations are usually necessary rather than sufficient. ([Department of Computer Science, HKU][3])

## Must be marked unverifiable

Examples:

* “Make the user happy” without access to the user’s later judgment.
* “This business strategy will succeed” without market outcomes.
* “This novel scientific claim is true” without experiments or accepted derivation.
* “This is more beautiful,” “more tasteful,” or “more persuasive” absent a calibrated preference model and target population.
* Private, inaccessible, or time-sensitive facts with no authoritative source.
* Goals whose success depends on delayed external consequences.

Unverifiable does not mean useless. It means **do not treat the result as ground truth training signal**.

---

# 3. Core data structures

```yaml
TaskSpec:
  task_id: str
  natural_goal: str
  context_artifacts: [ArtifactRef]
  allowed_tools: [ToolSpec]
  forbidden_side_effects: [Constraint]
  risk_class: low | medium | high
  required_abstention_conditions: [Condition]

Claim:
  claim_id: str
  task_id: str
  text: str
  type: output_property | factual | executable_behavior | state_delta |
        safety | preference | semantic_quality
  importance_weight: float
  critical: bool
  formalization: optional[Logic | TestSpec | Query | Assertion]
  observability: direct | indirect | unavailable
  unverifiable_reason: optional[str]

ValidatorSpec:
  validator_id: str
  authority_level: A0..A7
  claim_scope: [claim_id]
  input_contract: Schema
  execution_mode: deterministic | randomized | model_based | external_lookup
  sealed: bool
  leakage_risk: low | medium | high
  independence_group: str
  expected_error_model:
    false_accept_upper: float | unknown
    false_reject_upper: float | unknown
    calibration_domain: str
  health_requirements: [MetaTest]

ValidatorReport:
  validator_id: str
  run_id: str
  result: pass | fail | score | abstain | contradiction
  score: optional[float]
  raw_observation_ref: ArtifactRef
  confidence: float
  likelihood_ratio: float | interval
  coverage_contribution: dict[claim_id, float]
  provenance: [SourceRef]
  failure_mode_notes: [str]

EvidenceLedger:
  task_id: str
  run_id: str
  artifact_hash: str
  claims: [Claim]
  validator_reports: [ValidatorReport]
  claim_posteriors:
    claim_id:
      p_satisfied: interval
      authority_mass_by_level: dict
      coverage: float
      unresolved_conflicts: [str]
      unverifiable_residual: float
  hard_failures: [claim_id]
  sealed_eval_hash: str
  audit_status: clean | suspect | invalid

ComparisonRecord:
  parent_run_id: str
  child_run_id: str
  delta_distribution: Distribution
  expected_improvement: float
  lower_confidence_bound: float
  regression_risks: dict[str, float]
  decision: promote | reject | abstain | continue_sampling
  rationale: [str]

TrainingSignal:
  signal_id: str
  decision: promote | reject | abstain | explore_only
  target: strategy | tool_policy | prompt_pattern | reasoning_pattern | code_patch
  preference_pair: optional[{winner, loser, margin, confidence}]
  scalar_reward: optional[float]
  update_weight: float
  authority_summary: dict
  claim_feedback: [ClaimFeedback]
  redacted_counterexamples: [ArtifactRef]
  not_valid_for: [str]
```

The important design choice is that **reward is downstream of evidence**, not the other way around.

---

# 4. Goal-to-validator compiler

The compiler converts an open-ended goal into claims, proof obligations, tests, state assertions, and abstention conditions.

## Algorithm: `compile_goal_to_eval_plan`

```python
def compile_goal_to_eval_plan(task: TaskSpec) -> EvalPlan:
    # 1. Normalize the natural goal.
    objective = parse_goal(task.natural_goal, task.context_artifacts)

    # 2. Split into atomic claims.
    claims = decompose_into_claims(objective)

    # 3. Classify each claim.
    for c in claims:
        c.type = classify_claim(c)
        c.importance_weight = estimate_importance(c, task)
        c.critical = detect_safety_or_core_success(c)

    # 4. Attach strongest available validators.
    validators = []
    for c in claims:
        candidates = validator_registry.match(c, task)
        validators.extend(select_highest_authority_nonredundant(candidates))

    # 5. Generate partial validators for open-ended claims.
    for c in claims:
        if no_strong_validator(c):
            validators.extend(generate_partial_validators(c))
            if no_partial_validator(c):
                c.unverifiable_reason = explain_unverifiability(c)

    # 6. Validate the validators before use.
    health_reports = run_validator_health_suite(validators, claims)

    # 7. Remove or downgrade unhealthy validators.
    validators = filter_or_downgrade(validators, health_reports)

    # 8. Seal evaluation plan.
    return seal(EvalPlan(task, claims, validators, health_reports))
```

## Claim conversion examples

Open-ended goal:

> “Write a high-quality answer about database indexing.”

Compiled claims:

* The answer defines primary, secondary, composite, and covering indexes.
* It explains read/write tradeoffs.
* It does not claim false facts.
* It includes an example query plan or access pattern.
* It warns that indexes can slow writes and consume storage.
* It satisfies length and style constraints.
* “High quality” remains partly unverifiable unless there is a calibrated human-preference model for this domain.

Open-ended goal:

> “Improve this Python function.”

Compiled claims:

* Output code parses.
* Existing behavior is preserved on regression tests.
* New target behavior passes hidden tests.
* Complexity is no worse than baseline on benchmark distribution.
* No forbidden dependencies are introduced.
* Mutated buggy variants are killed by the test suite.
* Style/readability may be judged weakly unless a stronger rubric exists.

Mutation testing is useful for validator strength because it measures how well tests detect seeded faults; the mutation score is the ratio of detected seeded faults to total seeded faults. 

---

# 5. Validator-health and validator-strength system

A validator must itself be treated as an artifact under test.

## Validator health dimensions

```yaml
ValidatorHealth:
  soundness: estimated false-accept resistance
  sensitivity: ability to catch meaningful defects
  nonvacuity: fails empty, random, noop, and irrelevant outputs
  specificity: checks the intended claim rather than a proxy
  coverage: fraction of claim surface covered
  independence: not redundant with other validators
  leakage_resistance: hidden from solver and optimizer
  adversarial_resistance: survives red-team attempts
  calibration: empirical reliability on comparable historical tasks
  freshness: source/version recency where applicable
  reproducibility: stable under rerun, seed, and environment changes
```

## Health tests

A validator is downgraded or rejected if it fails any of these:

1. **Positive controls**: known-good outputs should pass.
2. **Negative controls**: known-bad outputs should fail.
3. **Mutation sensitivity**: small semantic bugs should be caught.
4. **Anti-vacuity checks**: empty output, boilerplate, irrelevant text, noop action, and copied prompt should fail.
5. **Leakage checks**: validator cannot inspect run ID, model identity, strategy metadata, hidden labels, or optimizer state.
6. **Overfit checks**: randomized equivalent cases and adversarial variants should not collapse performance.
7. **Bias checks** for learned judges: swap answer order, normalize length, blind model identity, vary style, test self-preference.
8. **Cross-validator disagreement checks**: if independent validators disagree, reduce confidence or abstain.

For property-style testing, randomized generation is valuable because it lets the system test properties over many generated cases rather than only hand-written examples; QuickCheck is an early example of this approach. 

---

# 6. Detecting weak, vacuous, leaky, overfit, or gameable validators

## Weak validator indicators

* High pass rate across bad baselines.
* Low mutation kill rate.
* Low claim coverage.
* Only checks format, length, or keyword presence.
* Cannot produce a counterexample.
* Strongly correlated with superficial features.

## Vacuous validator indicators

* Passes empty output.
* Passes a noop workflow.
* Passes because the tested condition is never reached.
* Uses an implication with false antecedent.
* Checks that “some citation exists,” not that the cited claim is supported.

## Leaky validator indicators

* Solver saw the hidden tests.
* Validator prompt includes the expected answer.
* Optimizer receives raw failed hidden cases and can memorize them.
* Validation depends on run metadata, model name, or strategy identity.
* Generated validator was conditioned on candidate output without adversarial health checks.

## Overfit validator indicators

* Passes fixed tests but fails generated variants.
* Performs well on public benchmark but poorly on newly sampled cases.
* Reward improves while independent validators degrade.
* Candidate contains phrases designed to satisfy judge rubrics rather than solve the task.

## Gameability test

Run an adversarial search:

```python
def redteam_validator(validator, claim):
    adversarial_outputs = generate_outputs_maximizing_validator_score(
        while_violating=claim
    )
    failures = []
    for y in adversarial_outputs:
        if validator(y).passes:
            failures.append(y)
    return failures
```

If the red team finds high-scoring false outputs, the validator is either downgraded or rejected.

This is especially important for learned judges. LLM-as-judge systems can correlate with human preferences, but documented limitations include position bias, verbosity bias, self-enhancement bias, and limited reasoning ability. ([arXiv][4])

---

# 7. Sealed evaluation protocol

The system uses three separated zones.

## Zone 1: Solver

The solver receives:

* Public task spec.
* Public constraints.
* Public examples if allowed.
* No hidden tests.
* No validator prompts.
* No validator seeds.
* No future optimizer update details.

## Zone 2: Evaluator

The evaluator receives:

* Task spec.
* Candidate artifact.
* Sealed validator plan.
* Hidden randomized tests.
* External source access when permitted.
* No strategy identity unless process validation requires it.

## Zone 3: Optimizer

The optimizer receives:

* Redacted training signal.
* Claim-level feedback.
* Aggregate authority/uncertainty.
* Counterexample summaries only when allowed.
* No raw hidden tests unless the test is retired.

## Protocol

```python
def sealed_eval(task, parent_strategy, child_strategy):
    eval_plan = compile_goal_to_eval_plan(task)
    commit_hash = hash(eval_plan)

    parent_run = run_solver(parent_strategy, task.public_view)
    child_run  = run_solver(child_strategy, task.public_view)

    parent_ledger = evaluate(parent_run, eval_plan)
    child_ledger  = evaluate(child_run, eval_plan)

    comparison = compare_ledgers(parent_ledger, child_ledger)

    signal = make_training_signal(comparison, redaction_policy=True)
    return signal
```

Key rule:

> A run may generate candidate validators, but it cannot be scored by its own validator unless that validator is independently validated, sealed, and either generated before the run output or blinded to candidate identity.

---

# 8. Evidence ledger

The evidence ledger is the main anti-fake-certainty mechanism.

It records:

* Which claims were checked.
* Which claims were not checkable.
* Which validator checked each claim.
* Authority level.
* Coverage.
* Error bounds.
* Provenance.
* Independence group.
* Leakage risk.
* Contradictions.
* Unverifiable residual.

Example ledger fragment:

```yaml
claim_posteriors:
  C1_behavior_correct:
    p_satisfied: [0.97, 0.995]
    authority_mass_by_level:
      A6: 0.70
      A5: 0.25
      A2: 0.00
    coverage: 0.83
    unresolved_conflicts: []
    unverifiable_residual: 0.17

  C2_code_readable:
    p_satisfied: [0.55, 0.78]
    authority_mass_by_level:
      A2: 0.40
      A1: 0.20
    coverage: 0.35
    unresolved_conflicts: ["judge disagreement"]
    unverifiable_residual: 0.65
```

A scalar score may be computed from this ledger, but the scalar is not authoritative by itself.

---

# 9. Evidence combination without laundering weak evidence

Each claim has a latent satisfaction variable:

[
Z_{r,c} \in {0,1}
]

for run (r) and claim (c).

Each validator emits an observation (o_v) with an estimated likelihood ratio:

[
LR_v = \frac{P(o_v \mid Z_{r,c}=1)}{P(o_v \mid Z_{r,c}=0)}
]

The system updates claim belief with clipped, authority-weighted log evidence:

[
\logit P(Z_{r,c}=1)
===================

\logit \pi_c
+
\sum_{g \in \text{independence groups}}
\text{fuse}_g
\left(
\kappa_v \cdot \text{clip}(\log LR_v, A_v)
\right)
]

Where:

* (A_v) is the validator’s authority level.
* (\kappa_v) is validator health.
* `clip` prevents weak validators from accumulating into fake certainty.
* `fuse_g` prevents double-counting correlated validators.

Within one independence group, use conservative fusion:

```python
def fuse_group(evidence_items):
    # Do not sum correlated judge outputs.
    # Use max strong evidence, or covariance-aware average if calibrated.
    return robust_upper_bounded_combination(evidence_items)
```

This means ten similar model judges do not become one proof.

---

# 10. Comparison algebra

A run is not “better” globally. It is better relative to a task distribution, claim weights, and acceptable uncertainty.

For each run:

[
Q_r = \sum_c w_c \cdot Z_{r,c} - \sum_s \lambda_s \cdot S_{r,s}
]

Where:

* (w_c) is claim importance.
* (Z_{r,c}) is claim satisfaction distribution.
* (S_{r,s}) is safety or side-effect violation.
* Hard critical failures can set (Q_r = -\infty) for promotion.

For parent (p) and child (n):

[
\Delta = Q_n - Q_p
]

The comparison result is a distribution, not a point estimate:

```yaml
Comparison:
  delta_mean: 0.084
  delta_ci_95: [-0.012, 0.171]
  p_child_better_than_margin: 0.91
  critical_regression_probability: 0.04
  decision: abstain
  reason: "Improvement plausible but lower bound below promotion margin."
```

## Dominance rules

1. **Hard safety fail dominates quality gain.**
2. **A7/A6 contradiction overrides A2/A1 approval.**
3. **Unverified claims do not become rewards.**
4. **Weak evidence can prioritize more evaluation, not final promotion.**
5. **Disagreement increases uncertainty; it does not average away.**

---

# 11. Statistical promotion rule

Promotion requires both **local run evidence** and **distributional evidence across tasks**.

For task (t_i), compute a paired difference:

[
d_i = E[Q_{child,i} - Q_{parent,i}]
]

with reliability weight (\rho_i), derived from authority, coverage, and validator health.

Effective sample size:

[
n_{\text{eff}} =
\frac{(\sum_i \rho_i)^2}{\sum_i \rho_i^2}
]

Promotion rule:

```python
def promotion_rule(comparisons):
    if any(child_has_critical_hard_failure(c) for c in comparisons):
        return REJECT

    if min_critical_claim_coverage(comparisons) < tau_coverage:
        return ABSTAIN

    if validator_health_floor(comparisons) < tau_health:
        return ABSTAIN

    mu_dist = estimate_weighted_mean_delta(comparisons)
    lcb = lower_confidence_bound(mu_dist, alpha=0.01)

    if lcb <= delta_min:
        return CONTINUE_OR_ABSTAIN

    if probability_of_regression_on_critical_slice(comparisons) > beta:
        return ABSTAIN

    if weak_evidence_fraction(comparisons) > max_weak_fraction:
        return EXPLORE_ONLY

    return PROMOTE
```

A concrete policy:

* Promote only if the lower confidence bound on expected improvement exceeds a minimum meaningful effect (\delta_{\min}).
* Reject if there is a critical hard failure.
* Abstain if evidence is weak, leaky, contradictory, under-covered, or mostly judge-based.
* Continue sampling if the evidence is promising but underpowered.
* Use fixed-confidence or anytime-valid procedures so repeated looks at the data do not create false promotions. Best-arm identification research formalizes the fixed-confidence goal as identifying the best arm with probability at least (1-\delta) while using as few samples as possible. 

---

# 12. Policy for weak signals

Weak signals include:

* Model judges.
* Critiques.
* Debate.
* Self-consistency.
* Process traces.
* Preference models.
* Reward models.

Policy:

1. **They may generate hypotheses.**
2. **They may route examples for stronger validation.**
3. **They may contribute bounded evidence if calibrated.**
4. **They may not certify truth, safety, or factuality alone.**
5. **They may not promote a strategy when stronger validators are absent for critical claims.**
6. **They must be audited for bias and leakage.**
7. **They must be domain-scoped.**

LLM judges are useful but should be treated as approximate preference estimators. Work on MT-Bench and Chatbot Arena found strong LLM judges could reach over 80% agreement with human preferences in that setting, while also documenting biases and reasoning limitations. ([arXiv][4])

For open-ended NLG evaluation, G-Eval-style systems can improve alignment with human judgments, but the cited work also reports only partial correlation and highlights bias toward LLM-generated text. ([ACL Anthology][5])

Therefore:

```yaml
weak_signal_policy:
  can_promote_alone: false
  can_break_tie: only_if_calibrated_and_noncritical
  can_request_more_tests: true
  can_generate_validators: true
  requires_bias_audit: true
  max_log_lr_contribution: small_bounded_value
```

---

# 13. Abstention path

The system abstains when any of these is true:

```yaml
AbstainIf:
  - critical_claim_unverified
  - validator_health_below_threshold
  - evidence_mostly_A1_or_A2
  - source_conflict_unresolved
  - leakage_risk_high
  - hidden_eval_compromised
  - child_improvement_below_min_effect_bound
  - posterior_expected_loss_too_high
  - unverifiable_residual_above_threshold
  - task_goal_ambiguous
  - factual_claim_requires_current_source_but_none_available
  - open_ended_quality_claim_without_calibrated_preference_model
```

Abstention emits useful signal:

```yaml
TrainingSignal:
  decision: abstain
  update_weight: 0
  useful_for:
    - validator_development
    - task_instrumentation
    - uncertainty_modeling
  reason:
    - "Core semantic quality claim unverifiable."
    - "Only model-judge evidence available."
    - "No independent source for factual claim."
```

Abstention is not failure. It is the system refusing to hallucinate reward.

---

# 14. Examples

## A. Software task

Task:

> “Implement an LRU cache with `get`, `put`, capacity eviction, and O(1) average operations.”

Compiled claims:

```yaml
claims:
  - API conforms to signature
  - get returns value or -1
  - put inserts and updates
  - least-recently-used key is evicted
  - capacity is never exceeded
  - average operation time is O(1)-like under benchmark
  - no forbidden dependencies
```

Validators:

* A6 hidden deterministic tests.
* A5 property tests: capacity invariant, update recency, random operation sequences compared to reference implementation.
* A5 mutation tests against likely bugs: evict most-recently-used, forget update recency, off-by-one capacity.
* A3 runtime/memory instrumentation.
* A1 style critique, not promotable.

Differential testing is useful when multiple backends or implementations should agree; in compiler testing, the same program can be compiled or interpreted under different settings, and divergent outputs indicate that at least one backend is wrong, assuming the test program is well-defined and deterministic. ([Ralf Jung][6])

Promotion:

* Child passes all critical A6 tests.
* Property tests find no counterexamples over sealed random seeds.
* Mutation score improves from 0.61 to 0.91.
* Benchmark regression probability is below threshold.
* Promote.

No promotion if the child only improves style judge score while failing a hidden eviction case.

---

## B. Factual task

Task:

> “Answer: what is the current policy limit for X?”

Compiled claims:

```yaml
claims:
  - identifies the correct jurisdiction/entity
  - retrieves current authoritative source
  - states policy limit exactly
  - includes effective date
  - does not overgeneralize beyond source scope
```

Validators:

* A4 official source lookup.
* A4 secondary independent source, if available.
* A3 retrieval provenance: timestamp, URL, document version.
* A2 claim verifier for source entailment.
* A1 critique for missing caveats.

Possible outcomes:

* Official source found and answer entailed: strong factual signal.
* Source conflict: abstain or return uncertainty.
* Only blog/forum source: partial or no promotion.
* No current source: unverifiable.

Training signal should update retrieval/source-selection strategy, not memorize the fact unless the system has a separate knowledge-ingestion protocol.

---

## C. Stateful tool workflow

Task:

> “Schedule a 30-minute meeting with Alex next Tuesday at 2 PM and attach the agenda.”

Compiled claims:

```yaml
claims:
  - correct calendar selected
  - event exists
  - attendee Alex added using resolved identity
  - start/end time correct in user timezone
  - agenda attached
  - no duplicate event created
  - no unrelated calendar changes
```

Validators:

* A3/A6 pre-state snapshot.
* A6 read-after-write calendar query.
* A6 attendee identity check.
* A6 attachment existence check.
* A3 side-effect diff.
* A5 idempotence test in sandbox if available.

Promotion:

* The workflow is better if the final state is correct, no side effects occurred, and the parent failed or required more steps.
* Abstain if Alex’s identity is ambiguous and no disambiguation was possible.
* Reject if the child created duplicates or modified unrelated events.

---

## D. Open-ended semantic task

Task:

> “Write a compelling product strategy memo.”

Compiled claims:

```yaml
claims:
  - includes target customer
  - identifies pain point
  - states product thesis
  - names risks
  - proposes measurable success metrics
  - avoids unsupported market-size claims
  - internally consistent
  - persuasive to intended audience
```

Validators:

* A6 schema/rubric coverage for required sections.
* A4 factual source checks for market claims.
* A5 consistency checks: no contradiction between target customer, pricing, and go-to-market.
* A2 calibrated judge for persuasiveness.
* A1 critique/debate for weaknesses.

Decision:

* Strongly validate factuality, structure, and consistency.
* Partially validate persuasiveness with calibrated preference model.
* Mark actual market success as unverifiable.
* Do not promote a strategy solely because the judge liked the memo.
* Use weak signal to select candidates for human review or later outcome validation.

---

# 15. Training-signal format

The optimizer should receive a signal like this:

```yaml
TrainingSignal:
  signal_id: TS-2026-00192
  task_id: LRU-cache-447
  parent_run: R_parent
  child_run: R_child

  decision: promote
  target:
    - code_synthesis_strategy
    - test_driven_revision_policy

  preference_pair:
    winner: R_child
    loser: R_parent
    margin_mean: 0.18
    margin_lcb_99: 0.04
    confidence: 0.991

  scalar_reward:
    value: 0.73
    valid: true
    derived_from: evidence_ledger_hash

  authority_summary:
    A6_direct_oracle: 0.62
    A5_property_testing: 0.31
    A2_learned_judge: 0.00
    unverifiable_residual: 0.07

  claim_feedback:
    - claim: eviction_correctness
      status: improved
      evidence: hidden_tests_and_property_tests
    - claim: operation_complexity
      status: unchanged
      evidence: benchmark_ci
    - claim: code_readability
      status: weak_positive
      evidence: model_judge
      usable_for_promotion: false

  redaction:
    hidden_tests_revealed: false
    counterexamples_revealed: summarized_only

  not_valid_for:
    - factual_world_knowledge_update
    - general_claim_that_child_is_better_on_open_ended_tasks
```

For abstention:

```yaml
TrainingSignal:
  decision: abstain
  update_weight: 0
  reason:
    - "Only A2 judge evidence for core quality claim."
    - "No authoritative source for factual subclaim."
  recommended_next_step:
    - "Create stronger source-grounded validator."
    - "Run calibrated human preference audit."
```

---

# 16. Main failure modes and mitigations

| Failure mode             | What happens                                 | Mitigation                                                           |
| ------------------------ | -------------------------------------------- | -------------------------------------------------------------------- |
| Specification gap        | Validator checks the wrong thing             | Claim ledger records unverifiable residual; no full-task promotion   |
| Reward hacking           | Solver learns proxy quirks                   | Sealed eval, adversarial validator red-teaming, evidence clipping    |
| Validator leakage        | Solver sees hidden tests or judge rubric     | Zone separation, hashing, redaction, retired tests                   |
| Weak evidence laundering | Many weak signals become fake certainty      | Authority caps, independence groups, no summing correlated judges    |
| Vague open-ended reward  | “Better” becomes judge preference            | Convert to claims; mark subjective residue                           |
| Overfitting to benchmark | Child improves on fixed cases only           | Randomized hidden tests, metamorphic tests, distributional promotion |
| Factual staleness        | Old source validates outdated claim          | Timestamped source provenance and freshness checks                   |
| Tool side effects        | Task succeeds while damaging unrelated state | Pre/post state diff, forbidden side-effect assertions                |
| Judge bias               | Longer or first answer wins                  | Position swaps, length normalization, model blinding                 |
| Optional stopping        | Repeated evals create false positives        | Fixed-confidence or anytime-valid promotion thresholds               |
| Process-trace deception  | Looks careful but wrong outcome              | Process traces cannot override outcome validators                    |
| Validator self-dealing   | Run writes validator it can pass             | Independent validator validation and sealing                         |
| Contradictory evidence   | Some validators pass, others fail            | Escalate uncertainty; abstain on critical conflicts                  |

---

# 17. The final rule

A run is allowed to improve the optimizer only when the system can produce this statement:

```text
For the task distribution D and claim set C,
the child strategy improves expected validated utility over the parent
by at least δ_min,
with confidence ≥ 1 - α,
under sealed validators whose authority, health, coverage, independence,
and leakage risk satisfy policy thresholds,
with no unresolved critical failures.
All remaining claims are explicitly marked partial or unverifiable.
```

Everything else becomes one of three non-promoting outcomes:

1. **Reject**: validated regression or hard failure.
2. **Continue sampling**: promising but underpowered.
3. **Abstain**: insufficient authority to know.

That is the key to extracting honest training signal: the system does not need universal automatic truth. It needs disciplined, claim-scoped validation that refuses to turn uncertainty into reward.

[1]: https://arxiv.org/html/2604.13602v1 "Reward Hacking in the Era of Large Models: Mechanisms, Emergent Misalignment, Challenges"
[2]: https://web.eecs.umich.edu/~weimerw/2025-481F/readings/testoracles.pdf "The Oracle Problem in Software Testing: A Survey"
[3]: https://www.cs.hku.hk/data/techreps/document/TR-2017-04.pdf "Metamorphic Testing: A Review of Challenges and Opportunities"
[4]: https://arxiv.org/abs/2306.05685 "[2306.05685] Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena"
[5]: https://aclanthology.org/2023.emnlp-main.153/ "G-Eval: NLG Evaluation using Gpt-4 with Better Human Alignment - ACL Anthology"
[6]: https://research.ralfj.de/papers/2024-oopsla-rustlantis.pdf "Rustlantis: Randomized Differential Testing of the Rust Compiler"
