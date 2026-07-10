# Prompt-Specific Oracle System Action Plan

## Purpose

This document designs the oracle system Agintor needs for prompt-specific autonomous improvement. It is not a checklist and it is not a hidden-answer toy design. The core problem is that Agintor is trying to evolve runtimes from user goals where no pre-existing training label exists. The only honest way to get useful training signal is to build a validation institution for each goal: decompose the goal into claims, create proof obligations, run validators under sealed authority, record typed evidence, and let search learn only from evidence whose authority, coverage, health, independence, and uncertainty are explicit.

The central invariant:

**No durable runtime promotion, archive insertion, scheduler credit, predictor label, or exported capability claim may be derived from a scalar reward unless that scalar is a projection from an evidence ledger with adequate authority for the claim being promoted.**

If Agintor cannot build such a ledger for a goal, the correct result is not fake confidence. The correct result is `abstain`, `validation_debt`, `human_audit_required`, or `exploration_only`.

## Research Consensus

Existing work points to the same design pressure from several directions:

| Research area | Relevant pattern | Agintor implication |
| --- | --- | --- |
| Reward hacking and scalable oversight | Proxy rewards get optimized against; evaluator access becomes part of the environment. Sources: [Concrete Problems in AI Safety](https://research.google/pubs/concrete-problems-in-ai-safety/), [Defining and Characterizing Reward Hacking](https://arxiv.org/abs/2209.13085), [Scaling Laws for Reward Model Overoptimization](https://arxiv.org/abs/2210.10760). | Treat every public score as hackable. Promotion authority must be sealed, isolated, and contamination-aware. |
| Holistic and behavioral evaluation | Single accuracy numbers miss robustness, calibration, fairness, efficiency, and behavioral failures. Sources: [HELM](https://arxiv.org/abs/2211.09110), [CheckList](https://aclanthology.org/2020.acl-main.442/). | The oracle must compile a multi-axis claim graph, not one benchmark score. |
| LLM judges | Model judges are useful approximators but biased by position, length, style, and self-preference. Sources: [Judging LLM-as-a-Judge](https://arxiv.org/abs/2306.05685), [Length-Controlled AlpacaEval](https://arxiv.org/abs/2404.04475). | LLM judges may guide exploration and provide weak evidence. They cannot alone certify capability promotion. |
| Agent benchmarks | Serious agent evals validate state changes, executable outcomes, traces, and resettable environments. Sources: [SWE-bench](https://arxiv.org/abs/2310.06770), [tau-bench](https://arxiv.org/abs/2406.12045), [WebArena](https://arxiv.org/abs/2307.13854), [OSWorld](https://arxiv.org/abs/2404.07972). | Agintor validators must run executable proof lanes: patch application/tests, stateful DB diffs, browser/OS state readbacks, and process-integrity checks. |
| Evaluation frameworks | Mature eval frameworks separate datasets, solvers, scorers, logs, tools, and sandboxes. Sources: [Inspect AI](https://inspect.aisi.org.uk), [OpenAI Evals](https://github.com/openai/evals), [OpenAI Evals API](https://developers.openai.com/api/reference/resources/evals). | Agintor should wrap external eval patterns into validator families but keep its own oracle authority, ledger, and promotion policy. |
| Sequential inference | Fixed-sample confidence intervals are invalid under repeated adaptive search. Sources: [confidence sequences](https://arxiv.org/abs/1810.08240), [game-theoretic anytime-valid inference](https://arxiv.org/abs/2210.01948), [SAFFRON online FDR](https://arxiv.org/abs/1802.09098), [Benjamini-Hochberg FDR](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x). | Promotion must use paired comparisons, anytime-valid bounds, and a global error budget. |
| Quality diversity and agent search | Agent design search needs archives and diversity, not one global best. Sources: [MAP-Elites](https://arxiv.org/abs/1504.04909), [Automated Design of Agentic Systems](https://arxiv.org/abs/2408.08435), [AgentOptimizer](https://arxiv.org/abs/2402.11359). | Agintor should search over architectures, prompts, tools, memory, and control policies, but credit them only through validated ArchitectureSignals. |

The consensus is not "make a better judge." It is "build a controlled measurement system that knows when it does not know."

## Current Repo Diagnosis

The current implementation has useful scaffolding but is not yet a reliable oracle system.

### What exists

- `agintor/contracts/oracle.py` defines `OraclePackage`, `OracleTask`, `ClaimSpec`, `ProofObligation`, `ValidatorSpec`, public projections, and freeze/load helpers.
- `agintor/oracle/compiler.py` compiles goal/runtime context into claims, validators, tasks, and an evidence contract.
- `agintor/oracle/validator_registry.py` and `agintor/oracle/families/*` define validator families.
- `agintor/evaluation/oracle_runner.py` executes validators and aggregates claim results.
- `agintor/evaluation/evaluator.py` substitutes oracle tasks and applies oracle scores before suite scoring.
- `agintor/evaluation/progress_oracle.py` is already the promotion authority.
- `agintor/search/archive.py` and `agintor/search/engine.py` already have quality-diversity and archive routing concepts.
- `agintor/runtime/langgraph/*`, `agintor/contracts/runtime_spec.py`, and `agintor/search/spec_mutator.py` create the beginning of spec-backed runtime search.

### What is missing

1. **Sealed payload integrity is broken.**
   `BenchmarkTask.private_expected` is excluded from serialization, while `freeze_oracle_package()` uses `model_dump(mode="json")`. That means nested sealed expected values can be lost before validators run. The compiler then selects `exact_private_answer` while the frozen task no longer contains the expected answer.

2. **Validator selection is not authority-aware enough.**
   The compiler emits broad validators like `repo_patch`, `exact_private_answer`, `trace_state`, `schema_artifact`, `consent_proof`, `factual_grounded`, and `stateful_service` based on generic context. Some selected validators have no real fixture or proof authority for the compiled task.

3. **QA accepts vacuous oracle packages.**
   `OracleQARunner` currently accepts packages where selected validators cannot meaningfully validate the claims. Positive/negative controls, leakage controls, and anti-vacuity controls are not hard prerequisites.

4. **Several validator families are artifact flag readers, not validators.**
   `repo_patch` reads fields like `applied`, `hidden_tests_passed`, and `tampered_tests`; it does not apply patches in a clean checkout, run tests, scan test tampering, or generate sealed logs. `stateful_service` and `trace_state` can pass if expected state or required events are absent.

5. **Evidence aggregation is too crude.**
   `OracleEvaluationRunner` collapses validator results into claim truth mostly through pass/fail/error. It does not model authority ceilings, validator health, independence groups, coverage, leakage risk, posterior intervals, or unverifiable residual mass.

6. **Search objectives do not come from the oracle.**
   `EvolutionEngine` still initializes objectives from `objective_specs_from_suite(suite, partition="train")`, while evaluation can substitute oracle tasks. The optimizer can therefore learn against stale suite-shaped objectives while the evaluator is using oracle-shaped tasks.

7. **Promotion statistics are not sequentially valid.**
   `ProgressOracle` uses ordinary paired effects and confidence intervals. Evolution repeatedly peeks, allocates compute adaptively, and evaluates many children. Fixed-sample inference is not safe in this regime.

8. **Scalar utility still has too much authority.**
   `ScoreCalculator.utility()` starts from `run.verifier_score` and subtracts cost/latency/fault penalties. That may remain useful for exploration, but not as promotion authority or durable learning signal.

9. **Architecture credit is under-specified.**
   Search knows which scopes and operators changed, but it does not produce a validated `ArchitectureSignal` with component effects, uncertainty intervals, confounds, and allowed optimizer updates.

10. **Runtime execution is not yet a strong evidence producer.**
    The current LangGraph operation layer is a beginning, but agent-like nodes and tool receipts do not yet emit the complete claim manifest, tool action ledger, state refs, and proof artifacts validators need.

These are architectural blockers, not polish issues. The system should not pretend they are solved by adding more benchmark tasks.

## End-State Architecture

The oracle is a factory-side and evaluator-side authority. The built runtime receives only public tasks and public feedback. It never receives private expected answers, sealed validator prompts, hidden seeds, fixture labels, or item-level promotion failures.

End-to-end flow:

1. Factory chat produces a `GoalSpec`.
2. `ValidationCompiler` compiles that goal into a `ValidationPlan`.
3. `ValidationPlan` contains claims, proof obligations, validator assignments, authority floors, sealed fixtures, public shaping tasks, held-out policy, contamination policy, and promotion policy.
4. `OraclePackage` freezes the plan into public and sealed projections.
5. Runtime evaluation gives the built runtime only public projections.
6. Runtime emits a `ClaimManifest`, artifacts, trace events, receipts, and declared residual uncertainty.
7. Validator runners execute outside the candidate workspace, with sealed fixtures and locked code.
8. `EvidenceLedger` records typed evidence rows and claim posterior intervals.
9. `ProgressOracle` consumes paired ledgers, not raw scores, and decides promote, reject, continue, abstain, or quarantine.
10. `ArchitectureSignal` converts promotion-grade comparisons into scoped learning updates for archive, scheduler, predictors, and mutator priors.
11. Exported runtimes carry a public proof bundle that states what was validated, at what authority, on which task classes, with which caveats.

The central object model:

| Object | Owner | Purpose |
| --- | --- | --- |
| `GoalSpec` | Factory | User-facing goal, constraints, domain, expected runtime family. |
| `TaskEnvelope` | Validation compiler | Runtime-visible task text plus allowed tools, forbidden actions, side-effect policy, risk class, and output contract. |
| `ValidationClaim` | Validation compiler | Atomic claim: outcome, state, process, safety, factual, semantic, preference, or architecture. |
| `ProofObligation` | Validation compiler | Check that would make a claim observable. |
| `ValidatorSpec` | Oracle | Validator family, authority ceiling, required inputs, independence group, leakage risk, health requirements. |
| `ValidationPlan` | Oracle | The complete goal-conditioned validation contract. |
| `OraclePackage` | Oracle | Frozen public and sealed projections with hashes and provenance. |
| `ClaimManifest` | Runtime | Runtime's explicit declaration of what it claims to have achieved and what evidence refs support it. |
| `ValidatorReport` | Evaluator | Typed validator output for one claim/run with status, interval, authority used, health, coverage, and leakage flags. |
| `EvidenceLedger` | Evaluator | Append-only evidence object for one run/task/runtime hash. |
| `ComparisonRecord` | Progress oracle | Paired parent-child comparison over the same task/seed/projection. |
| `PromotionDecision` | Progress oracle | promote, reject, continue, abstain, or quarantine with reason codes. |
| `ArchitectureSignal` | Search | Component-level learning record derived from promotion-grade evidence. |

## Mathematical Design

### 1. Goal-conditioned validation

Let `p` be the user's prompt or follow-up, `g` the normalized goal, and `R_h` a runtime candidate with genome hash `h`.

The compiler constructs:

$$
V(g) = (T_g, C_g, O_g, J_g, P_g)
$$

where:

- `T_g` is the set of public and sealed task envelopes.
- `C_g` is the claim graph.
- `O_g` is the set of proof obligations.
- `J_g` is the validator assignment.
- `P_g` is the promotion policy.

Each claim `c in C_g` has:

$$
c = (type, criticality, weight, authority_floor, observability, scope, dependencies)
$$

Criticality is one of `hard_gate`, `major`, `minor`, or `diagnostic`. A hard gate is not averaged into a score. If it fails, the run is invalid or quarantined.

### 2. Validator evidence

For a run `r`, task `t`, and claim `c`, validator `v_j` receives only its authorized inputs:

$$
e_{r,t,c,j} = v_j(public_artifact, trace, receipts, sealed_fixture_j)
$$

The report contains:

$$
e_{r,t,c,j} = (status, x, [l,u], A_j, H_j, coverage, independence_group, leakage_flags)
$$

where:

- `status` is pass, fail, score, abstain, contradiction, error, or quarantine.
- `x in [-1,1]` is a bounded evidence contribution if the validator can score the claim.
- `[l,u]` is the validator's own interval when applicable.
- `A_j` is nominal authority.
- `H_j` is health.
- `coverage` is how much of the claim surface this validator checks.

### 3. Authority and health

Use two related authority scales.

Claim-level authority:

| Level | Meaning | Examples |
| --- | --- | --- |
| `A0` | No evidence | Unobservable or purely subjective claim. |
| `A1` | Heuristic critique | Self-critique, debate trace, style review. |
| `A2` | Calibrated learned evaluator | LLM judge, reward model, preference model. |
| `A3` | Grounded trace/state evidence | Tool receipts, citation checks, process logs, state readbacks. |
| `A4` | Executable partial oracle | Unit tests, property tests, public simulators, schema/diff checks. |
| `A5` | Sealed independent oracle | Hidden tests, private state, sealed randomized fixtures, external source of truth. |
| `A6` | Formal or certified proof | Proof checker, model checker, type/property certificate. |

Promotion authority:

| Level | Meaning |
| --- | --- |
| `M0` | No optimizer update allowed. |
| `M1` | Exploration only. |
| `M2` | Weak preference or judge-calibration update only. |
| `M3` | Grounded subskill/process update. |
| `M4` | Local capability update for the checked claim class. |
| `M5` | Strong capability promotion inside a sealed task class. |
| `M6` | Certified invariant or formal promotion. |

Validator health is a minimum over required health channels:

$$
H_j = min(nonvacuity, sensitivity, specificity, coverage, reproducibility, calibration, independence, leakage_resistance, architecture_neutrality, cost_fairness)
$$

Effective authority is capped:

$$
A^{eff}_j = min(A^{nom}_j, authority_cap(H_j), leakage_cap_j, applicability_cap_{c,j})
$$

A validator cannot contribute authority above the property it actually checks. A hidden unit test can validate a patch outcome; it does not validate factual truth in a paragraph. A formal proof of the wrong invariant is still wrong.

### 4. Conservative evidence fusion

For each claim `c`, the ledger stores an interval, not a point:

$$
B_{r,t,c} = [P^-_{r,t,c}, P^+_{r,t,c}]
$$

Evidence is fused by independence group, not by blindly summing validators:

$$
L_c = logit(pi_c) + sum_{g in G_c} clip(conservative\_group\_evidence(g), cap(A^{eff}_g))
$$

$$
P^-_c = sigmoid(L^-_c), \quad P^+_c = sigmoid(L^+_c)
$$

The fusion rules:

- Validators in the same independence group are treated as correlated.
- Weak learned judges are clipped at low authority unless calibrated on fresh controls.
- Contradictions widen intervals or trigger quarantine.
- Missing required evidence increases unverifiable residual mass.
- Leakage flags override ordinary scoring.

The point estimate used for exploration can exist, but the authoritative object is the interval plus authority mass:

$$
authority\_mass_c[A] = sum_{j: A^{eff}_j=A} coverage_{c,j} * H_j
$$

### 5. Utility projection

For one run:

$$
U^-_r = I(hard\_gates\_pass) * \left( \sum_{c \in C} w_c P^-_{r,c} - cost\_penalty_r - latency\_penalty_r - risk\_penalty_r \right)
$$

$$
U^+_r = I(hard\_gates\_not\_failed) * \left( \sum_{c \in C} w_c P^+_{r,c} - lower\_bound\_penalties_r \right)
$$

This gives the search loop pessimistic and optimistic views. Promotion uses pessimistic child versus optimistic parent:

$$
d_i = U^-_{child,i} - U^+_{parent,i}
$$

Exploration may use softer point scores, but any durable update must cite the interval and authority profile.

### 6. Paired promotion under optional stopping

Agintor search is adaptive. It repeatedly evaluates candidates, peeks at intermediate results, chooses new mutations based on those results, and tests many hypotheses. Fixed-horizon confidence intervals are not valid here.

For each candidate and promotion axis `a`, define paired deltas:

$$
d_i(c,b,a) = score_i(candidate, a) - score_i(baseline, a)
$$

Each pair must share:

- task identity
- seed or scenario identity
- public projection hash
- sealed fixture digest
- validator bundle digest
- runtime environment digest

For bounded `d_i in [L_a, U_a]`, use an anytime-valid lower confidence bound. A conservative MVP-safe form is:

$$
delta_t = delta_a * 6 / (\pi^2 t^2)
$$

$$
LCB_t = mean_t - (U_a-L_a) * sqrt(log(1/delta_t) / (2t))
$$

Because `sum_t delta_t <= delta_a`, repeated peeking remains controlled. Later, replace this with empirical-Bernstein or betting/e-process confidence sequences without changing the interface.

When task reliabilities differ:

$$
mean^w_t = \frac{\sum_i rho_i d_i}{\sum_i rho_i}
$$

$$
n_{eff} = \frac{(\sum_i rho_i)^2}{\sum_i rho_i^2}
$$

where `rho_i` is derived from authority mass, coverage, validator health, and leakage cleanliness.

Capability promotion requires:

$$
LCB_t(mu_{cap}) > epsilon_{cap}
$$

and all of the following:

- hard gates pass
- no leakage or evaluator tampering
- minimum effective authority per critical claim is met
- minimum coverage per critical claim is met
- protected regression guards satisfy `LCB_t(mu_g) > -eta_g`
- validator health floor is met
- unverifiable residual mass is below threshold
- alpha budget is available

If any hard integrity condition fails, the decision is `quarantine`, not "low score." If evidence is insufficient but clean, the decision is `continue` or `abstain`.

### 7. Global error budget

Each factory chat owns an error budget:

$$
alpha_{global}
$$

Every promotion hypothesis receives an alpha allocation chosen before seeing that candidate's sealed result:

$$
sum_j alpha_j <= alpha_{global}
$$

For an open-ended stream, use alpha-wealth:

- Start with initial wealth `W_0`.
- Spend `alpha_j` on candidate `j`.
- Earn bounded wealth only when a promotion is accepted under the rule.
- Thresholds depend only on past evidence, never current hidden outcomes.

This prevents the evolutionary loop from discovering false positives by repeated experimentation.

### 8. Architecture credit

Promotion says a child runtime is better than a parent on a validated axis. It does not automatically say which component caused the gain.

Let a child differ by mutation set:

$$
K = \{k_1, ..., k_m\}
$$

For component `k`, the ideal Shapley effect is:

$$
phi_k = E_{S \subseteq K \setminus \{k\}} [U(S \cup \{k\}) - U(S)]
$$

Agintor cannot run all `2^m` coalitions at scale, so it uses:

- direct credit for one-mutation children
- targeted factorial ablations for 2 to 4 mutations
- fractional design-of-experiments for 5 or more mutations
- sampled Shapley only for promising/confounded bundles
- "bundle only" credit when components were never separated

Each component effect is interval-valued:

$$
effect_k = [LCB(phi_k), UCB(phi_k)]
$$

The scheduler may update a component prior only when the interval supports the update at the required authority. Otherwise it records `confounded_with` and schedules later ablations.

## Component Design

### 1. ValidationCompiler

New owning module: `agintor/oracle/compiler.py` plus a split into `agintor/oracle/claim_compiler.py`, `agintor/oracle/proof_planner.py`, and `agintor/oracle/task_generators/*` when the file becomes too broad.

Inputs:

- `GoalSpec`
- runtime family/spec
- allowed tools and side-effect policy
- user risk class
- domain hints
- existing factory chat history

Outputs:

- `ValidationPlan`
- public task set
- sealed task set
- validator bundle
- health suite
- promotion policy
- abstention policy

Responsibilities:

- Decompose the goal into atomic claims.
- Mark criticality and dependencies.
- Determine which claims are observable.
- Assign authority floors.
- Select validators only when their required inputs exist.
- Generate public shaping tasks separately from sealed promotion tasks.
- Emit explicit unverifiable residuals.
- Create positive/negative/canary controls for every validator family.

Compiler invariants:

- No selected validator may have unsatisfied input requirements.
- No hard claim may have `A0` authority without making the whole plan `human_audit_required` or `exploration_only`.
- No private fixture may appear in public projection.
- Every validator must fail at least one known-bad control before it can contribute promotion evidence.
- If no sealed evidence exists, `private_expected_available` must be false.

### 2. OraclePackage and Projection System

Owning module: `agintor/contracts/oracle.py`, with serialization helpers pulled into `agintor/oracle/package_io.py` if needed.

Required projections:

| Projection | Visible to runtime | Visible to evaluator | Purpose |
| --- | --- | --- | --- |
| Public | yes | yes | Task text, output schema, allowed tools, public tests, public rubric categories. |
| Sealed | no | yes | Hidden expected values, private states, seeds, judge prompts, thresholds, canaries. |
| Audit | no, except exported summary | yes | Hashes, provenance, validator versions, health results, leakage attestations. |
| Export proof | yes after build | yes | Public statement of validation authority and caveats. |

Current fix required:

- Stop using generic `model_dump()` as the sealed serialization path for objects with excluded private fields.
- Implement an explicit sealed serializer that calls `sealed_benchmark_task_payload()` for nested tasks.
- Add a public serializer that proves private fields are absent.
- Add a round-trip test: compile, freeze, load, run exact sealed validator, and verify private expected survives only in sealed evaluator context.

### 3. Runtime Evidence Protocol

Owning modules: `agintor/runtime/sdk/entrypoint.py`, `agintor/runtime/langgraph/*`, `agintor/contracts/evidence.py`.

The built runtime should return more than an answer. For every task, it emits:

- final artifact refs
- `ClaimManifest`
- tool action ledger
- file write ledger
- memory read/write ledger
- side-effect receipts
- declared abstentions
- declared unverifiable residuals
- trace digest
- runtime spec digest

The runtime is allowed to say "I cannot prove this claim." That is valuable evidence. It prevents false confidence and gives the factory a validator-improvement target.

Required manifest fields:

| Field | Meaning |
| --- | --- |
| `manifest_id` | Stable digest of task, runtime hash, and claimed artifacts. |
| `task_id` | Public task identity. |
| `runtime_hash` | Built runtime identity. |
| `claims` | Runtime-declared atomic claims mapped to oracle claim IDs where possible. |
| `artifact_refs` | Output artifacts and typed schemas. |
| `evidence_refs` | Trace, receipt, citation, test, or state refs. |
| `tool_actions` | Canonical tool calls with args digest and outputs digest. |
| `side_effects` | External or local side effects. |
| `abstentions` | Claims the runtime did not satisfy or cannot prove. |
| `residuals` | Claims outside current observable authority. |

Runtime invariant:

The runtime never sees sealed answers, hidden tests, validator prompts, sealed thresholds, or private failure labels. It may receive aggregate public feedback such as "claim class failed under A4 public tests" but not item-level sealed failures.

### 4. Validator Runner Layer

Owning modules: `agintor/oracle/families/*`, `agintor/evaluation/oracle_runner.py`, new `agintor/evaluation/validator_runtime.py`.

Validators are not score functions. They are controlled measurement procedures.

Each validator family must define:

- required public inputs
- required sealed inputs
- authority ceiling
- health suite
- independence group
- leakage risks
- result schema
- control cases
- quarantine triggers

Runner requirements:

- execute outside candidate workspace
- use locked validator code
- mount sealed fixtures read-only
- make candidate artifacts read-only
- recompute metrics from raw artifacts
- capture logs and command digests
- detect candidate writes to score files/tests/fixtures
- blind candidate identity where possible
- support parallel validator execution

### 5. EvidenceLedger

Owning module: new `agintor/contracts/validation.py` or extension of `agintor/contracts/evidence.py`.

The ledger is the training-signal source. It stores:

- `ledger_id`
- `oracle_package_hash`
- `validation_plan_hash`
- `public_projection_hash`
- `sealed_projection_hash`
- `runtime_hash`
- `task_id`
- `run_id`
- `claim_manifest_digest`
- `validator_reports`
- `claim_posteriors`
- `authority_mass`
- `coverage`
- `independence_partition`
- `leakage_attestation`
- `process_violations`
- `side_effect_violations`
- `unverifiable_residual`
- `audit_status`

Ledger invariant:

No consumer reads a naked `verifier_score` for promotion. Consumers read ledgers, claim intervals, authority summaries, and decision records.

### 6. Claim Inference Engine

Owning module: new `agintor/evaluation/claim_inference.py`.

Responsibilities:

- Convert validator reports into claim posterior intervals.
- Enforce authority floors.
- Enforce health floors.
- Fuse correlated validators conservatively.
- Widen intervals when evidence conflicts.
- Mark claims as abstained when coverage is insufficient.
- Quarantine on leakage or tampering.
- Produce conservative utility projections.

Decision states per claim:

| State | Meaning |
| --- | --- |
| `satisfied` | Required authority and lower bound met. |
| `failed` | Required claim failed. |
| `uncertain` | Evidence clean but interval spans threshold. |
| `abstained` | Validator applicability or coverage insufficient. |
| `quarantined` | Integrity risk: leakage, tampering, contamination, or suspicious process. |
| `unverifiable` | No current proof obligation can observe this claim. |

### 7. ProgressOracle

Owning module: `agintor/evaluation/progress_oracle.py`, with math extracted to new `agintor/evaluation/promotion.py`.

Inputs:

- parent ledgers
- child ledgers
- comparison design
- promotion policy
- alpha budget state
- protected-slice policy

Outputs:

- `PromotionDecision`
- `ComparisonRecord`
- `ArchitectureSignal` eligibility summary

Decision rule:

- Reject on hard failure.
- Quarantine on leakage, tampering, private authority mismatch, or evaluator integrity failure.
- Continue if clean evidence is promising but underpowered.
- Abstain if evidence cannot reach required authority under the current plan.
- Promote only when anytime-valid lower bounds clear thresholds and protected regressions are ruled out.

### 8. Search Integration

Owning modules: `agintor/search/engine.py`, `agintor/search/archive.py`, `agintor/search/spec_mutator.py`, `agintor/learning/*`.

Search must receive two distinct signals:

1. **ExplorationSignal**
   - can include weak scores, public tests, cheap diagnostics, LLM judges, novelty, cost, and trace summaries
   - may guide what to try next
   - cannot promote durable capability

2. **ArchitectureSignal**
   - derived from promotion-grade comparisons
   - carries authority mass, claim intervals, scope, mutation IDs, confounds, and allowed updates
   - may update archives, scheduler, predictors, and mutator priors

`ArchitectureSignal` fields:

| Field | Meaning |
| --- | --- |
| `parent_runtime_hash` | Baseline runtime. |
| `child_runtime_hash` | Candidate runtime. |
| `oracle_package_hash` | Validation authority. |
| `comparison_design_id` | Paired task/seed design. |
| `axis` | capability, efficiency, subskill, preference, safety, process. |
| `decision` | promote, reject, continue, abstain, quarantine. |
| `authority_profile` | M-level and A-level mass. |
| `effect_interval` | Overall paired effect interval. |
| `protected_regressions` | Regression guard intervals. |
| `mutation_actions` | Exact topology/prompt/tool/memory/control changes. |
| `component_effects` | Per-component intervals when identifiable. |
| `confounds` | Components that cannot be separated yet. |
| `allowed_updates` | archive, scheduler, predictor, mutator prior, export claim. |

Archive design:

- Keep quality-diversity, but add authority and risk dimensions.
- A cell should not contain one blended winner only. It should contain a Pareto front over capability lower bound, cost, latency, leakage risk, complexity, and authority profile.
- A high A2 score cannot displace an A5-validated runtime in the same capability cell.

### 9. Validator Health System

Owning module: new `agintor/evaluation/validator_health.py`.

Every validator family has a health suite:

- positive controls
- negative controls
- mutation sensitivity
- anti-vacuity controls
- leakage controls
- contamination controls
- architecture-neutrality controls
- order/length/style bias controls for judges
- flake/reproducibility checks
- cost fairness checks
- adversarial score-gaming cases

Validator health produces:

$$
H_j = min(h_1, ..., h_n)
$$

If `H_j` drops below the required floor:

- the validator can still log diagnostics
- it cannot contribute promotion authority
- existing promotions that relied on that validator are marked for re-audit

### 10. Oracle Improvement Loop

The oracle is not static. When a goal cannot be validated, Agintor should improve the validation system, not pretend the runtime improved.

New artifact: `ValidationDebtItem`.

Fields:

- goal hash
- blocked claim
- missing proof obligation
- missing validator family or fixture generator
- risk class
- suggested proof lane
- expected authority if implemented
- owner workstream

Examples:

- "Need sealed stateful service simulator for refund-policy workflow."
- "Need citation-span checker for factual compliance claim."
- "Need patch tamper scanner before repo_patch can be M5."
- "Need human rubric calibration before preference claim can leave M2."

This lets factory follow-ups answer honestly: "Agintor can explore designs, but it cannot yet certify this claim without adding a validator."

## Domain Proof Lanes

### Lane 0: Protocol Validation Lane

Purpose: prove the oracle machinery works, not prove product capability.

Use generated deterministic tasks with sealed expected outputs, public projections, canaries, and known-bad runtimes. This lane is allowed only to validate:

- serialization integrity
- public/sealed split
- validator execution
- evidence ledger formation
- promotion math
- leakage quarantine

It must not be used to claim real-world runtime competence.

### Lane 1: Repo Patch

This should be the first high-authority real proof lane.

Design:

- frozen repo snapshot
- base commit digest
- issue prompt
- writable path policy
- public tests for shaping
- sealed hidden tests for promotion
- patch application in clean copy
- dependency/environment digest
- test command digest
- test tamper scan
- forbidden-file scan
- diff-size and scope policy
- logs stored as evidence artifacts

Validators:

- patch applies cleanly
- public tests pass
- hidden tests pass
- no protected tests/fixtures modified
- no forbidden paths changed
- no broad unrelated rewrite unless policy permits
- no generated fake report files used as truth
- trace shows candidate did not read sealed data

Authority:

- public tests: A4/M4 local
- hidden tests in clean evaluator: A5/M5 for repo_patch claims
- static process checks: A3-A4 depending on coverage

### Lane 2: Stateful Service

This should be the second high-authority lane.

Design:

- initial DB state
- domain policy document
- typed API tools
- simulated user or event script
- operation log
- expected final private state
- duplicate side-effect detector
- policy-violation checker
- resettable fixture
- repeated seeds for reliability

Validators:

- final DB state equals allowed target state
- no forbidden operations
- side effects are idempotent under retry
- policy constraints satisfied
- user-visible response matches resulting state
- no direct DB writes unless tool policy permits

Authority:

- state diff with sealed expected state: A5/M5
- public API receipts: A3-A4
- LLM user simulator transcript quality: A2 diagnostic unless separately calibrated

### Lane 3: Factual and Grounded Claims

Use for factual, research, summarization, citation, and compliance tasks.

Design:

- required claim extraction
- source retrieval policy
- source freshness policy
- citation-span mapping
- contradiction search
- source independence check
- optional human/expert audit

Validators:

- citations exist and support the claim
- dates and quoted spans match sources
- no unsupported critical claim
- contradiction check against trusted sources
- freshness requirements met

Authority:

- citation-span and metadata checks: A3-A4 for groundedness
- external authoritative database/API: A5 for narrow facts
- LLM judge summary quality: A2 only

Important caveat:

Groundedness is not truth unless the source is authoritative for the claim. The oracle must distinguish "cited" from "correct."

### Lane 4: Browser and OS Tasks

This lane should remain scaffold-only until reset, state readback, and programmatic validators are reliable.

Design:

- frozen local web app or VM state
- deterministic reset
- browser/OS trace
- final DOM/DB/file assertion
- screenshot evidence
- accessibility tree evidence
- forbidden action scanner

Validators:

- final state reached
- no forbidden destructive action
- no credential/secret leakage
- task-specific script passes
- trace contains allowed action sequence

Authority:

- execution-based script and state assertion: A4-A5
- screenshot/visual judge: A2-A3 unless backed by programmatic state

### Lane 5: Trading and Time-Series Tasks

Use only with strict time controls.

Design:

- cutoff timestamp
- market data snapshot digest
- allowed data sources
- policy/risk constraints
- event replay
- realized outcome after horizon
- transaction-cost model
- risk-adjusted score
- leakage/future-data scanner

Validators:

- no future data access
- orders match allowed policy
- realized PnL/risk computed from sealed post-cutoff data
- drawdown and exposure limits respected
- reasoning artifacts do not claim unavailable facts

Authority:

- realized sealed outcome: A5 for narrow forecast/trading metrics
- model judge on rationale: A2 diagnostic
- backtest only: A3-A4 depending on leakage controls

### Lane 6: Human and Preference Tasks

Use for claims that are actually subjective or policy-laden.

Design:

- rubric
- blinded pairwise presentation
- randomized order
- length/style normalization where possible
- adjudication protocol
- calibration items
- inter-rater agreement
- human signoff digest

Authority:

- calibrated preference model: A2
- blinded expert/human audit: A5 for the audited preference claim only

Rule:

Preference wins do not imply capability wins unless tied to independent outcome evidence.

## Current Blocker Solutions

### Blocker 1: Sealed expected values disappear

Solution:

- Create explicit sealed serialization for `OraclePackage`.
- Serialize nested benchmark tasks through `sealed_benchmark_task_payload()`.
- Keep public serialization strict and private-free.
- Add a package integrity check: every selected sealed validator with `requires private_expected` must have a sealed payload after freeze/load.
- Add a regression test that fails today: compile package, freeze, load, run `exact_private_answer` with matching artifact, and require pass.

Definition of done:

- public projection contains no private fields
- sealed projection preserves private fields
- package hash differentiates sealed payload digest without exposing values
- QA fails if a selected validator requires missing sealed inputs

### Blocker 2: Compiler claims private expected availability unconditionally

Solution:

- Replace context booleans with fixture capability records.
- `private_expected_available` is true only when a sealed fixture exists and round-trips.
- `exact_private_answer` applicability returns zero unless its required sealed input is present.
- Compiler records why a validator was selected for each claim.

Definition of done:

- no exact/private validator appears in a plan without corresponding sealed fixture
- validator selection audit explains applicability and authority floor

### Blocker 3: QA passes vacuous validators

Solution:

- `OracleQARunner` must run validator controls, not only schema checks.
- Required controls: known-good, known-bad, empty artifact, irrelevant artifact, leakage canary.
- A validator family that cannot run controls is diagnostic-only.

Definition of done:

- a package with `trace_state` and no required events fails QA or downgrades to diagnostic
- a package with `stateful_service` and no expected state cannot contribute capability authority
- QA output lists authority downgrades and blocked claims

### Blocker 4: Validators read artifacts instead of validating

Solution:

- Move high-authority validators into runner-backed implementations.
- `repo_patch` must apply patches and run tests in evaluator-controlled clean copies.
- `stateful_service` must run API scripts and compare final private state.
- `trace_state` must require explicit event obligations and fail absence when obligations exist.

Definition of done:

- candidate cannot pass by writing `hidden_tests_passed: true`
- evaluator recomputes results from raw artifacts and sealed fixtures
- reports include command digests and logs

### Blocker 5: Evidence aggregation launders weak signals

Solution:

- Add `ClaimInferenceEngine`.
- Replace boolean claim aggregation with interval, authority mass, coverage, health, and residual mass.
- LLM judges are capped at A2 unless they produce checkable subclaims verified by stronger validators.

Definition of done:

- an A2 judge cannot satisfy an A5 authority floor
- conflicting validators widen uncertainty or trigger quarantine
- claim result explains which authority floor was met or missed

### Blocker 6: Search objectives are suite-shaped while eval tasks are oracle-shaped

Solution:

- Build objective specs from `OraclePackage.evidence_contract` and `ValidationPlan`, not only from the original suite.
- Keep suite objectives only when no oracle package exists.
- Store objective provenance in search state.

Definition of done:

- stage-4 oracle task IDs and search objective IDs are aligned
- archive axes include authority profile and risk profile
- objective mismatch is a hard QA failure

### Blocker 7: Promotion statistics ignore optional stopping

Solution:

- Add `AnytimeConfidenceSequence`.
- Add `AlphaBudget`.
- Make `ProgressOracle` promotion decisions cite the comparison design, alpha spent, effective sample size, and lower bound.
- Keep fixed-sample summaries as diagnostics only.

Definition of done:

- repeated peeking tests do not inflate false promotions beyond configured alpha in simulation
- promotion cannot run without alpha allocation
- protected regression guards use the same paired evidence discipline

### Blocker 8: Architecture credit is not identifiable

Solution:

- Add `ArchitectureSignal`.
- Track mutation action IDs from `spec_mutator.py`.
- If a child has multiple mutations, store bundle credit unless ablations separate effects.
- Schedule ablations as part of the search plan.

Definition of done:

- scheduler credit is no longer `delta / len(scope)`
- component priors update only from identifiable or explicitly bundle-scoped evidence
- confounded components are recorded and later separated

### Blocker 9: Runtime does not emit enough evidence

Solution:

- Make `ClaimManifest` mandatory for promotion-grade runs.
- Bind LangGraph node inputs, outputs, tool calls, memory refs, and receipts into the evidence protocol.
- Tool-not-bound and side-effect no-op receipts are diagnostic failures for tool-use claims.

Definition of done:

- runtime output includes manifest, artifact refs, receipts, trace digest, and residuals
- validators can check claims without scraping freeform prose
- missing manifest means exploration-only score

## Implementation Plan

### Phase 0: Stop false authority

Goal: prevent the current oracle from producing misleading promotion signal.

File scopes:

- `agintor/contracts/oracle.py`
- `agintor/contracts/benchmarks.py`
- `agintor/oracle/compiler.py`
- `agintor/oracle/qa.py`
- `agintor/evaluation/oracle_runner.py`
- `agintor/search/engine.py`
- `tests/test_langgraph_oracle_pass1.py`

Work:

- Implement sealed/public package serializers.
- Make private fixture availability real, not assumed.
- Downgrade or reject inapplicable validators.
- Make QA fail on missing sealed inputs and vacuous validator plans.
- Align search objectives with oracle task/evidence axes.

Acceptance tests:

- private expected survives freeze/load only in sealed evaluator context
- public projection leak test stays strict
- exact private validator is not selected without sealed expected
- QA rejects vacuous `trace_state` and `stateful_service`
- search objective IDs match oracle evaluation axes

### Phase 1: First-class validation contracts

Goal: make the validation plan the actual authority object.

File scopes:

- new `agintor/contracts/validation.py`
- `agintor/contracts/oracle.py`
- `agintor/contracts/evidence.py`
- `agintor/oracle/compiler.py`
- `agintor/factory/pipeline.py`
- `agintor/factory/export.py`
- storage models that persist oracle refs

Work:

- Add `ValidationPlan`, `ValidationClaim`, `ProofObligation`, `ValidatorHealth`, `ValidatorReport`, `ClaimPosterior`, `EvidenceLedger`, `ComparisonRecord`, `ArchitectureSignal`, and `AlphaBudget`.
- Freeze hashes for plan, public projection, sealed projection, validator bundle, and fixtures.
- Preserve `BenchmarkPlan` as task-selection metadata only.
- Add projection round-trip tests.

Definition of done:

- every oracle package has a validation plan hash
- every task has claim/proof-obligation coverage or explicit residuals
- no scalar score can be marked promotion-authoritative without a ledger

### Phase 2: Validator health and control suites

Goal: validators become measured artifacts.

File scopes:

- new `agintor/evaluation/validator_health.py`
- `agintor/oracle/families/*`
- `agintor/oracle/validator_registry.py`
- `agintor/oracle/qa.py`

Work:

- Define health suites per family.
- Add positive, negative, anti-vacuity, mutation, leakage, and neutrality controls.
- Compute health caps and authority downgrades.
- Make QA include control execution.

Definition of done:

- no validator can contribute above diagnostic authority without passing controls
- health result is stored in oracle package audit metadata
- failed health controls downgrade or block promotion

### Phase 3: Evidence ledger and claim inference

Goal: replace scalar-first scoring with evidence-first evaluation.

File scopes:

- new `agintor/evaluation/claim_inference.py`
- `agintor/evaluation/oracle_runner.py`
- `agintor/evaluation/evaluator.py`
- `agintor/evaluation/scoring.py`
- `agintor/contracts/evidence.py`

Work:

- Emit validator reports as typed rows.
- Build claim posterior intervals.
- Track authority mass, coverage, independence, and residuals.
- Project utility from ledgers.
- Keep legacy `verifier_score` as a compatibility projection only.

Definition of done:

- evaluator writes ledgers for oracle runs
- `verifier_score` is traceable to ledger projection
- promotion code can run without reading raw scalar verifier scores

### Phase 4: Repo patch proof lane

Goal: first high-authority real-world lane.

File scopes:

- `agintor/oracle/families/repo_patch.py`
- new `agintor/evaluation/runners/repo_patch_runner.py`
- fixture definitions under `agintor/evaluation/fixtures/` or a generated runtime artifact directory
- tests under `tests/`

Work:

- Create frozen repo fixture contract.
- Apply candidate patches in clean evaluator workspace.
- Run public and sealed tests.
- Detect test/fixture tampering.
- Store logs and command digests.

Definition of done:

- known-good patch passes
- hidden-test regression fails
- test tampering quarantines
- artifact flag spoofing cannot pass

### Phase 5: Stateful service proof lane

Goal: second high-authority lane for tool-using agents.

File scopes:

- `agintor/oracle/families/stateful_service.py`
- new service fixture generator/runner
- runtime tool receipt contracts
- tests

Work:

- Define initial state, API tools, policy doc, user script, and expected final private state.
- Execute candidate through runtime host.
- Compare final state and side effects.
- Add repeated seeds and pass-k reliability summaries.

Definition of done:

- final state is checked from evaluator-side DB readback
- duplicate side effects fail
- policy violations fail even if final text is persuasive
- user simulator transcript alone cannot pass

### Phase 6: Runtime evidence protocol

Goal: built runtimes become evidence producers.

File scopes:

- `agintor/runtime/sdk/entrypoint.py`
- `agintor/runtime/langgraph/*`
- `agintor/contracts/runtime.py`
- `agintor/contracts/evidence.py`
- templates/baseline runtime artifacts as needed

Work:

- Add `ClaimManifest`.
- Bind LangGraph node inputs and outputs into trace state.
- Emit canonical tool receipts and side-effect records.
- Make missing receipts explicit failures for tool-use claims.
- Preserve product split: runtime chat sessions are normal sessions, benchmark tasks are evaluation machinery.

Definition of done:

- every promotion-grade run emits a manifest
- validators can consume typed evidence without parsing freeform text
- runtime-visible tasks never include sealed fields

### Phase 7: Anytime promotion and alpha budget

Goal: make promotion statistically valid under adaptive search.

File scopes:

- new `agintor/evaluation/promotion.py`
- `agintor/evaluation/progress_oracle.py`
- `agintor/contracts/search.py`
- `agintor/search/engine.py`
- search state persistence

Work:

- Implement conservative anytime confidence sequence.
- Implement alpha budget ledger.
- Require paired comparison designs.
- Add protected regression guards.
- Add simulation tests for false promotion rate.

Definition of done:

- promotion decision includes alpha spent, effective sample size, LCB/UCB, and reason codes
- optional stopping simulation respects alpha
- no candidate can spend hidden eval attempts indefinitely

### Phase 8: ArchitectureSignal and credit assignment

Goal: search learns from validated component effects, not child score deltas.

File scopes:

- `agintor/search/spec_mutator.py`
- `agintor/search/engine.py`
- `agintor/search/archive.py`
- `agintor/learning/observations.py`
- `agintor/learning/predictors.py`
- new `agintor/search/credit_assignment.py`

Work:

- Assign mutation action IDs.
- Generate comparison designs for ablations.
- Emit `ArchitectureSignal`.
- Store confounds.
- Add archive authority/risk dimensions.
- Gate scheduler and predictor updates by allowed update scopes.

Definition of done:

- one-mutation child gets direct interval credit
- multi-mutation child gets bundle credit unless ablated
- component prior cannot update from confounded evidence
- archive preserves A5-validated designs against A2-only displacement

### Phase 9: Oracle improvement queue

Goal: make missing validators a first-class product outcome.

File scopes:

- new `agintor/oracle/validation_debt.py`
- factory service/follow-up paths
- Dev Docs deferred ledger integration where needed

Work:

- Emit `ValidationDebtItem` when claims cannot be validated.
- Surface bounded product language in factory chat.
- Route non-current issues to `Dev Docs/DEFERRED_ISSUES_LEDGER.md`.

Definition of done:

- "cannot validate" produces a durable debt item, not a fake score
- factory follow-up can ask for a stronger validator/proof lane
- exported runtimes include validation caveats

### Phase 10: Export proof bundle

Goal: exported runtimes say what they are and are not validated to do.

File scopes:

- `agintor/factory/export.py`
- `agintor/runtime/sdk/bundle.py`
- runtime metadata contracts

Work:

- Include public proof bundle with validation authority, task classes, version hashes, and caveats.
- Exclude sealed payloads.
- Include reproducibility metadata for evaluator reruns.

Definition of done:

- exported runtime cannot leak sealed fixtures
- user-visible metadata states validation authority by claim class
- rerun instructions point to evaluator-side sealed package refs where available

## Acceptance Criteria

The oracle system is complete enough for WS4 readiness only when all of the following are true:

1. **Public/sealed split is mechanically enforced.**
   Public projections cannot contain private expected values, hidden seeds, sealed validator prompts, thresholds, or private fixtures.

2. **Every promotion-grade claim has proof authority.**
   A claim without a proof obligation is residual, not silently scored.

3. **Validators are health-audited.**
   No validator contributes promotion evidence without positive, negative, anti-vacuity, mutation, and leakage controls.

4. **Evidence is typed and ledgered.**
   Promotion decisions cite evidence ledgers, not raw `verifier_score`.

5. **LLM judges are capped.**
   They cannot certify capability alone. They can guide exploration, flag issues, and propose checkable subclaims.

6. **Promotion is paired and anytime-valid.**
   Parent and child comparisons use matched task/seed/projection/evaluator identities, confidence sequences, and alpha budget.

7. **Search learns only from allowed authority.**
   Exploration can use weak signals. Archive insertion, scheduler credit, predictor labels, and mutator priors require `ArchitectureSignal`.

8. **Architecture credit is identifiable or marked confounded.**
   Multi-mutation gains do not update individual component priors unless ablations or design-of-experiments support the attribution.

9. **Evaluator integrity failures quarantine.**
   Leakage, tampering, private-authority mismatch, or suspicious validator execution never become low scores.

10. **Missing validation becomes product-visible debt.**
    The factory can say, plainly, that a runtime can be built but not certified for a claim until a validator exists.

## Regression Test Matrix

| Test | Expected result |
| --- | --- |
| Compile/freeze/load package with sealed expected answer | sealed evaluator can read it; public projection cannot. |
| Exact private validator selected with no sealed input | QA fails. |
| Empty artifact against `trace_state` with no required events | diagnostic only or QA fail; never capability pass. |
| `repo_patch` artifact sets `hidden_tests_passed=true` without patch run | fail. |
| Candidate modifies tests/fixtures | quarantine. |
| Hidden expected appears in runtime trace/artifact | quarantine. |
| LLM judge prefers verbose but wrong answer | no capability promotion; judge health records bias. |
| Repeated peeking simulation with null candidates | false promotions stay within alpha. |
| Child improves cost but regresses primary quality | no efficiency promotion unless regression guard passes. |
| Multi-mutation child wins without ablations | bundle credit only; components marked confounded. |
| Search objective IDs differ from oracle task/evidence axes | QA fail. |
| Export runtime after sealed validation | public proof bundle present; sealed payload absent. |

## What This Means Product-Wise

Agintor should not claim it can extract training signal from thin air. It can do something more defensible:

1. Turn a user goal into explicit claims.
2. Decide which claims can be observed.
3. Build or select validators for those claims.
4. Run those validators outside the runtime's control.
5. Learn only from validated evidence.
6. Admit when a goal is not yet certifiable.

That is the real oracle system. The novelty is not a magic scorer. The novelty is prompt-specific validator construction plus evidence-governed evolutionary search.

## Implementation Order Summary

The fastest safe order is:

1. Fix sealed package integrity and validator applicability.
2. Make QA fail vacuous authority.
3. Add `ValidationPlan` and `EvidenceLedger` schemas.
4. Replace boolean claim aggregation with claim intervals and authority mass.
5. Harden `repo_patch` into a real evaluator-runner.
6. Add runtime `ClaimManifest` and typed receipts.
7. Replace fixed promotion intervals with anytime-valid paired promotion.
8. Route search through `ArchitectureSignal`.
9. Add stateful service lane.
10. Add validation debt and export proof bundles.

This order prevents the current system from learning false lessons while still preserving the existing product spine: factory builds runtimes; built runtimes solve sessions; benchmark tasks and oracle packages are evaluator machinery.

## Source Bibliography

- Amodei et al., [Concrete Problems in AI Safety](https://research.google/pubs/concrete-problems-in-ai-safety/).
- Skalse et al., [Defining and Characterizing Reward Hacking](https://arxiv.org/abs/2209.13085).
- Gao, Schulman, Hilton, [Scaling Laws for Reward Model Overoptimization](https://arxiv.org/abs/2210.10760).
- Liang et al., [Holistic Evaluation of Language Models](https://arxiv.org/abs/2211.09110).
- Ribeiro et al., [Beyond Accuracy: Behavioral Testing of NLP Models with CheckList](https://aclanthology.org/2020.acl-main.442/).
- Zheng et al., [Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685).
- Dubois et al., [Length-Controlled AlpacaEval](https://arxiv.org/abs/2404.04475).
- Jimenez et al., [SWE-bench](https://arxiv.org/abs/2310.06770).
- Yao et al., [tau-bench](https://arxiv.org/abs/2406.12045).
- Zhou et al., [WebArena](https://arxiv.org/abs/2307.13854).
- Xie et al., [OSWorld](https://arxiv.org/abs/2404.07972).
- UK AI Security Institute, [Inspect AI](https://inspect.aisi.org.uk).
- OpenAI, [Evals repository](https://github.com/openai/evals) and [Evals API](https://developers.openai.com/api/reference/resources/evals).
- Howard et al., [Time-uniform, nonparametric, nonasymptotic confidence sequences](https://arxiv.org/abs/1810.08240).
- Ramdas et al., [Game-theoretic statistics and safe anytime-valid inference](https://arxiv.org/abs/2210.01948).
- Ramdas et al., [SAFFRON: Online FDR control](https://arxiv.org/abs/1802.09098).
- Benjamini and Hochberg, [Controlling the False Discovery Rate](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x).
- Mouret and Clune, [Illuminating search spaces by mapping elites](https://arxiv.org/abs/1504.04909).
- Hu, Lu, Clune, [Automated Design of Agentic Systems](https://arxiv.org/abs/2408.08435).
- Zhang et al., [Offline Training of Language Model Agents with Functions as Learnable Weights](https://arxiv.org/abs/2402.11359).
- Dudik, Langford, Li, [Doubly Robust Policy Evaluation and Learning](https://arxiv.org/abs/1103.4601).
