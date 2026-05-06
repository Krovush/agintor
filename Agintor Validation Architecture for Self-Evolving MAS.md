e remedy is not better metrics. The remedy is the disciplined refusal to accept a metric without its provenance
# Agintor Validation Architecture for Self-Evolving Multi-Agent Systems

## Abstract

Agintor should not optimize "better answers." It should optimize validated design changes to built runtimes. The object of search is a multi-agent-system genome: workflow graph, template family, agent roles, memory policy, tool policy, coordination protocol, model allocation, and budget policy. A scalar score is not the reward source. It is only a projection of a sealed, claim-level evidence ledger whose authority, coverage, health, independence, leakage risk, and uncertainty are explicit.

The central architecture is a Claim-Authority Validation Engine for MAS search:

```text
GoalSpec
-> TaskEnvelope
-> ClaimGraph
-> ValidationContract
-> frozen BenchmarkPlan + VerifierBundle + fixtures
-> frozen MAS genome
-> sealed paired evaluation
-> EvidenceLedger
-> ComparisonRecord
-> scoped OptimizerUpdate or abstention
```

This document synthesizes the two automatic-validation source papers into an Agintor-specific theory of optimizer-safe validation. It also incorporates recent work on automated design of agentic systems, agentic workflow search, self-evolving multi-agent architecture search, graph-based MAS construction, verifier-guided self-evolution, agent-generated tool curation, adversarially safe MAS design, reward hacking, software oracle theory, metamorphic testing, differential testing, and LLM judge limitations. The practical conclusion is narrow and strict: Agintor may evolve aggressively only where evidence is strong, evolve locally where evidence is partial, and abstain where the task objective cannot be grounded.

## 1. Research Position

Recent agentic-systems research supports Agintor's premise that the agent architecture itself should be searched rather than handcrafted. ADAS frames agentic systems as designs that can be invented and recombined automatically, including code-defined agents discovered by a meta-agent. AFlow models agentic workflows as code graphs and optimizes them with search and execution feedback. AutoMaAS applies self-evolving multi-agent architecture search with operator lifecycle management, cost-aware optimization, online feedback, and decision tracing. MASFactory models LLM-based MAS workflows as directed computation graphs compiled from natural-language intent into executable workflow specifications. SAGE shows a verifier-guided multi-agent self-evolution loop where generated tasks and plans are filtered to maintain signal quality. ALITA-G evolves reusable tools from successful trajectories and curates them into a retrievable tool box. MaMa adds an adversarial designer/adversary game for automatically designing MAS structures that remain safe under compromised agents.

The common lesson is not "let agents self-improve." The lesson is "represent the evolving system explicitly, evaluate it under externally grounded evidence, and prevent the optimizer from mistaking evaluator artifacts for the task objective."

Reward hacking research makes the danger structural: when an expressive policy is optimized against a compressed proxy, it will tend to exploit the compression under sufficient search pressure. Software testing research calls the same problem the oracle problem: observing behavior is not enough unless the system has a trustworthy way to decide whether the behavior is correct. LLM-as-judge work is useful for scalable preference approximation, but its own authors document position bias, verbosity bias, self-enhancement bias, incomplete reasoning, partial correlation with humans, and model-output bias. Therefore, in Agintor, learned judges and internal critiques are weak evidence unless calibrated against higher-authority signals.

Agintor needs a validation architecture, not a larger benchmark list.

## 2. Unit of Optimization

Agintor's factory produces a built runtime. The runtime is what users later chat with or run on benchmark tasks. Benchmark tasks are evaluation machinery, not product-facing prompt categories. The optimizer therefore updates beliefs over runtime-design choices, not over isolated completions.

The optimized object should be a typed MAS genome:

```python
MASGenome = {
    "genome_id": "genome.<hash>",
    "template_card": "planner_executor_reviewer",
    "workflow_graph": {
        "nodes": ["planner", "executor", "state_auditor", "critic", "synthesizer"],
        "edges": [
            ["planner", "executor"],
            ["executor", "state_auditor"],
            ["state_auditor", "critic"],
            ["critic", "executor"],
            ["critic", "synthesizer"],
        ],
    },
    "agent_specs": {
        "planner": {"role_prompt_hash": "...", "model": "...", "temperature": 0.2},
        "executor": {"role_prompt_hash": "...", "tools": ["filesystem", "pytest"]},
        "state_auditor": {"role_prompt_hash": "...", "tools": ["diff", "readback"]},
    },
    "coordination_policy": {
        "handoff_protocol": "structured_claims",
        "max_review_loops": 2,
        "termination_rule": "critical_claims_checked_or_abstain",
    },
    "memory_policy": {
        "shared_memory": True,
        "private_scratchpads": True,
        "retrieval_scope": "task_local",
    },
    "tool_policy": {
        "side_effect_mode": "sandbox_until_commit",
        "requires_pre_action_plan": True,
        "requires_post_action_readback": True,
    },
    "budget_policy": {
        "max_tokens": 120000,
        "max_tool_calls": 80,
        "max_wall_time_s": 600,
    },
}
```

A valid Agintor training statement has this shape:

```text
For task class D, under validation contract V, the child genome G_child
improves expected validated utility over parent genome G_parent by at least
delta_min with confidence 1 - alpha, after cost, latency, side-effect risk,
unverifiable residual, leakage risk, and validator health are accounted for.
The update is scoped to the genome components and task classes supported by
the evidence.
```

Anything weaker is not a promotion signal. It may be diagnostic, exploration guidance, or validation debt.

## 3. Validation Contract

The missing Agintor primitive is a `ValidationContract`. `BenchmarkPlan` chooses task instances. `VerifierBundle` names executable checks. Neither object alone states the claim-level semantics that make optimizer updates trustworthy.

The contract should be frozen before candidate execution:

```python
ValidationContract = {
    "contract_id": "validation.<hash>",
    "goal_id": "...",
    "task_class": "repo_patch | service_task | structured_ops | factual | semantic_open",
    "public_task_view_hash": "...",
    "claim_graph_hash": "...",
    "benchmark_plan_id": "...",
    "verifier_bundle_id": "...",
    "fixture_bundle_id": "...",
    "authority_policy": {
        "minimum_for_promotion": "V5",
        "minimum_for_process_update": "V3",
        "max_weak_evidence_fraction": 0.25,
    },
    "abstention_policy": {
        "critical_claim_unverified": "abstain",
        "high_leakage_risk": "quarantine",
        "unverifiable_residual_max": 0.20,
    },
    "sealing_policy": {
        "hidden_seed_commit": "after_genome_commit",
        "raw_hidden_failures_visible_to_optimizer": False,
        "retire_compromised_cases": True,
    },
}
```

The contract separates four questions:

| Question | Agintor object |
| --- | --- |
| What is being claimed? | `ClaimGraph` |
| Which task instances exercise those claims? | `BenchmarkPlan` and `BenchmarkPartitionEntry` |
| Which validators may observe which artifacts? | `VerifierBundle` plus visibility policy |
| What optimizer update is legal? | `ComparisonRecord` and `OptimizerUpdate` |

Without this contract, a higher `verifier_score` can silently become an illegitimate global reward.

## 4. Claim-Local Authority

Authority is local to a claim. A hidden test can strongly validate a behavior claim while saying nothing about user satisfaction. A proof checker can validate a formal invariant while saying nothing about whether the invariant captures the user's intent. A model judge can approximate preference but cannot override a failing executable oracle.

Agintor should use this authority ladder:

| Level | Authority class | Validates | Optimizer rights |
| --- | --- | --- | --- |
| `V0` | Unverifiable | Subjective, future-dependent, inaccessible, underspecified, or ambiguous claims | No update. Record residual and request instrumentation or human feedback. |
| `V1` | Trace plausibility | The run looked structured: planned, debated, reflected, or followed a visible protocol | Diagnostics only. No promotion. |
| `V2` | Heuristic or learned critique | LLM judge, self-critique, committee vote, preference model, style rubric | Exploration, triage, candidate ranking, tie-breaks only inside noncritical claims. |
| `V3` | Instrumented process/state evidence | Tool logs, trace events, state snapshots, side-effect receipts, access-control checks | Process-policy updates only; cannot prove outcome value. |
| `V4` | External factual authority | Official source, timestamped document, canonical dataset, independent citation | Strong for bounded factual claims; weak for interpretation. |
| `V5` | Partial executable oracle | Unit tests, property tests, metamorphic relations, differential checks, static checks, state assertions | Strong for covered claims, with coverage and health limits. |
| `V6` | Sealed independent oracle | Hidden tests, private cases, simulator with stable semantics, authoritative API readback, reference implementation | Strong architecture update for covered task class if health and leakage gates pass. |
| `V7` | Formal certificate | Type proof, theorem-prover certificate, proof-carrying code, cryptographic proof | Strongest for formalized property only; cannot certify an incomplete spec. |

The anti-laundering rule:

```text
Many V1/V2 validators cannot sum into V5/V6 authority.
Weak evidence may reduce uncertainty inside its authority cap, but it cannot
raise the authority ceiling of the claim.
```

## 5. Claim Graph

A MAS run must expose what it wants credit for. Opaque artifacts are not enough. Agintor should require a claim graph for each evaluation unit.

Core claim classes:

| Claim class | Examples | Typical high-authority validators |
| --- | --- | --- |
| Outcome claim | Patch fixes bug, answer states correct value, final report contains required fields | Hidden tests, exact expected output, source entailment, reference implementation |
| State claim | Calendar event exists once, file changed only in declared targets, database row updated | Pre/post state diff, read-after-write, idempotence check, side-effect audit |
| Process claim | Runtime used allowed tools, did not access hidden eval, did not modify tests | Trace audit, filesystem access audit, sandbox receipt reconciliation |
| Architecture claim | Reviewer subgraph caused improvement under equal budget | Paired parent/child evaluation, ablation, mutation lineage, counterfactual reverts |
| Search-validity claim | Improvement generalizes beyond sampled proxy tasks | Held-out evaluation, contamination checks, fixed-confidence sampling, leakage audit |

Minimal schema:

```python
class Claim:
    claim_id: str
    task_id: str
    text: str
    claim_type: str
    critical: bool
    importance_weight: float
    formalization: dict | None
    observability: str  # direct | indirect | unavailable
    minimum_authority_for_promotion: str
    verifier_refs: list[str]
    unverifiable_reason: str | None


class ProofObligation:
    obligation_id: str
    claim_id: str
    required_authority: str
    accepted_validator_families: list[str]
    coverage_threshold: float
    health_threshold: float
    leakage_threshold: str
    failure_action: str  # reject | abstain | quarantine


class ClaimGraph:
    graph_id: str
    root_goal_id: str
    claims: list[Claim]
    dependencies: list[tuple[str, str]]
    critical_claim_ids: list[str]
    unverifiable_residual_claim_ids: list[str]
```

Open-ended tasks are not excluded. They are decomposed. Factual subclaims, structural subclaims, consistency subclaims, and state subclaims can be validated. The remaining semantic-quality residual is marked as weak or unverifiable unless a calibrated human/preference oracle exists.

## 6. Validator Health

A validator is itself an artifact under test. Its declared authority is only an upper bound. Health can downgrade authority; it cannot upgrade it.

```text
effective_authority =
    min(
        declared_authority,
        health_cap,
        coverage_cap,
        leakage_cap,
        calibration_cap,
        independence_cap,
    )
```

Validator health dimensions:

| Dimension | Meaning |
| --- | --- |
| Soundness | Resistance to false accepts |
| Sensitivity | Ability to catch meaningful defects |
| Nonvacuity | Fails empty, noop, copied prompt, irrelevant output, unreachable assertion |
| Specificity | Checks the intended claim instead of a superficial proxy |
| Coverage | Fraction of the critical claim surface covered |
| Independence | Not redundant with another validator or generated from the same failure mode |
| Leakage resistance | Hidden cases, seeds, prompts, labels, and canaries are not visible to solver or optimizer |
| Adversarial resistance | Separate red-team search cannot pass while violating the claim |
| Calibration | Error model estimated on comparable tasks |
| Freshness | Source, dependency, environment, and fixture recency where applicable |
| Reproducibility | Stable under rerun, seed, backend, and environment changes |

MAS-specific health dimensions:

| Dimension | Meaning |
| --- | --- |
| Architecture neutrality | Rewards outcome validity rather than a favored topology, verbosity, or planning style |
| Cost fairness | Normalizes or penalizes extra agents, tokens, wall time, and tool calls |
| Topology invariance | Another valid topology can pass if it solves the task |
| Anti-theatre sensitivity | Distinguishes real verification from critique-looking prose |
| Tool-grounding sensitivity | Catches hallucinated tool results, missing readbacks, and invalid receipts |
| Coordination-failure sensitivity | Catches role collapse, duplicate actions, dead agents, infinite loops, and unmerged subresults |
| Collusion resistance | Prevents solver, critic, and validator proposal agents from sharing hidden evaluation state |

Every serious validator should face:

1. Positive controls.
2. Negative controls.
3. Mutation tests over likely faults.
4. Metamorphic variants when exact outputs are unavailable.
5. Differential checks against independent implementations or backends when possible.
6. Anti-vacuity cases.
7. Leakage and canary checks.
8. Bias checks for learned judges: answer order swaps, length normalization, model blinding, style perturbation.
9. Adversarial solver challenge: generate outputs that maximize validator score while violating the claim.
10. Coverage mapping against success criteria and claim graph.

Generated validators are allowed only as proposals. A candidate run may propose a test, fixture, or rubric, but it cannot be scored by that validator until the validator is independently audited, sealed, and blinded to candidate identity.

## 7. Sealed Evaluation Protocol

Agintor needs four zones.

```text
Factory Search Zone
  chooses or mutates MAS genome
  sees public task view and prior sanitized ledgers
  does not see hidden tests, canaries, judge prompts, or raw hidden failures

Runtime Runner Zone
  executes parent and child genomes through RuntimeHost / runtime entrypoint
  sees public task view only
  emits artifact, trace, state refs, and claim manifest

Evaluator Zone
  sees sealed ValidationContract, hidden seeds, fixtures, validators, and candidate artifacts
  writes EvidenceLedger and redacted counterexample summaries

Optimizer Zone
  sees ComparisonRecord and OptimizerUpdate
  never sees raw hidden tests unless those tests are retired
```

Protocol:

```python
def sealed_paired_evaluation(goal, parent_genome, child_genome):
    contract = compile_and_freeze_validation_contract(goal)
    parent_commit = commit_genome(parent_genome, contract.public_task_view_hash)
    child_commit = commit_genome(child_genome, contract.public_task_view_hash)

    hidden_eval = sample_or_reveal_hidden_eval_after_commit(contract)

    parent_run = run_runtime(parent_commit, contract.public_task_view)
    child_run = run_runtime(child_commit, contract.public_task_view)

    parent_ledger = evaluate_with_sealed_validators(parent_run, hidden_eval)
    child_ledger = evaluate_with_sealed_validators(child_run, hidden_eval)

    comparison = compare_ledgers(parent_ledger, child_ledger, contract)
    return make_redacted_optimizer_update(comparison)
```

Hard rules:

1. Freeze the validation contract before execution.
2. Freeze the MAS genome hash before hidden seeds are sampled or revealed.
3. Hide validator prompts, hidden tests, reference outputs, and canaries from all agents.
4. Retire any hidden case exposed for debugging.
5. Quarantine, not merely penalize, candidates that access evaluator state, modify tests, read canaries, tamper with logs, or alter scorer behavior.
6. Mutation prompts may contain sanitized failure classes and trace references, never raw hidden tests or verifier outputs.
7. Evaluation must route through `RuntimeHost` and the runtime entrypoint. Factory code must not import runtime-kernel internals to shortcut evaluation.

## 8. Evidence Ledger and Fusion

The evidence ledger is the anti-fake-certainty mechanism. It records which claims were checked, by which validators, with what authority, coverage, health, independence, and uncertainty.

```python
class EvidenceLedger:
    ledger_id: str
    contract_id: str
    run_id: str
    genome_hash: str
    artifact_hash: str
    claim_reports: list[ClaimReport]
    validator_reports: list[ValidatorReport]
    hard_failures: list[str]
    unresolved_conflicts: list[str]
    unverifiable_residual: float
    leakage_status: str  # clean | suspect | compromised
    audit_status: str    # clean | invalid | quarantine
```

For claim `c`, let `Z_c` be latent satisfaction of that claim. A validator `v` emits observation `o_v` with an estimated likelihood ratio. Evidence is combined with authority caps, health weights, and independence groups:

```text
logit P(Z_c = 1 | O)
  = logit prior_c
    + sum_over_independence_groups(
        conservative_fusion(
          health_v * clip(log_likelihood_ratio_v, authority_cap_v)
        )
      )
```

Conservative fusion means:

1. Do not sum correlated judges.
2. Do not let many weak validators create proof-level certainty.
3. Treat high-authority contradictions as dominance events, not as averageable noise.
4. Increase interval width when independent validators disagree.
5. Keep unverifiable residual explicit.

The scalar score is downstream:

```text
scalar_score = projection(EvidenceLedger, scoring_policy)
```

It is valid only when accompanied by the ledger hash and authority summary.

## 9. Comparison Algebra

Agintor compares genomes under paired conditions. It does not compare isolated scores.

For run `r` on task instance `i`:

```text
Q_{r,i}
  = sum_c weight_c * E[Z_{r,i,c}]
    - lambda_cost * normalized_cost_{r,i}
    - lambda_latency * normalized_latency_{r,i}
    - lambda_risk * side_effect_risk_{r,i}
    - lambda_unverified * unverifiable_residual_{r,i}
```

Critical hard failures set promotion eligibility to false even if `Q` is high.

For parent `p` and child `n`:

```text
d_i = Q_{n,i} - Q_{p,i}
rho_i = reliability_weight(authority, health, coverage, leakage, independence)

n_eff = (sum_i rho_i)^2 / sum_i rho_i^2
delta_lcb = lower_confidence_bound(weighted_mean(d_i, rho_i), alpha)
delta_ucb = upper_confidence_bound(weighted_mean(d_i, rho_i), alpha)
```

Decision table:

| Condition | Decision |
| --- | --- |
| Hidden eval leakage, canary access, scorer/test tampering | `quarantine` |
| Critical safety, state, or tool invariant fails | `reject` |
| Effective authority below policy floor for critical claims | `abstain` |
| Unverifiable residual above policy threshold | `abstain` |
| Evidence is mostly V1/V2 for core objective | `shadow_only` |
| `delta_lcb > delta_min` under strong validators and no critical regression | `promote` |
| `delta_ucb < -delta_reject` | `reject` |
| Promising but underpowered | `continue_sampling` |
| Mixed improvements by task class | `conditional_promote` with scoped routing |

Pseudocode:

```python
def decide_promotion(parent_ledger, child_ledger, contract):
    comparison = compare_claims(parent_ledger, child_ledger, contract)

    if comparison.leakage_or_tampering:
        return Quarantine(comparison)

    if comparison.child_has_critical_hard_failure:
        return Reject(comparison)

    if comparison.min_effective_authority < contract.minimum_for_promotion:
        return Abstain("authority floor not met", comparison)

    if comparison.critical_claim_coverage < contract.coverage_floor:
        return Abstain("critical claim under-covered", comparison)

    if comparison.unverifiable_residual > contract.max_unverifiable_residual:
        return Abstain("unverifiable residual too high", comparison)

    if comparison.weak_evidence_fraction > contract.max_weak_evidence_fraction:
        return ShadowOnly(comparison)

    if comparison.regression_probability_on_critical_slice > contract.max_regression_probability:
        return Abstain("critical regression risk too high", comparison)

    if comparison.delta_lcb > contract.delta_min:
        return Promote(comparison)

    if comparison.delta_ucb < -contract.delta_reject:
        return Reject(comparison)

    return ContinueSamplingOrAbstain(comparison)
```

This should replace the conceptual role currently played by raw `verifier_score` deltas and simple lower-confidence-bound gates. Those gates are useful mechanics, but they do not carry authority, leakage, coverage, or abstention semantics by themselves.

## 10. Optimizer Update Rules

The optimizer should receive structured updates, not naked rewards.

```python
class OptimizerUpdate:
    decision: str  # promote | conditional_promote | reject | abstain | shadow_only | quarantine
    scope: dict
    parent_genome_hash: str
    child_genome_hash: str
    validated_delta_interval: tuple[float, float]
    authority_summary: dict[str, float]
    evidence_ledger_refs: list[str]
    component_credit: list[dict]
    allowed_updates: list[str]
    forbidden_generalizations: list[str]
    validation_debt: list[str]
```

Allowed effects:

| Decision | Archive insertion | Scheduler credit | Predictor training | Template/knob prior update |
| --- | --- | --- | --- | --- |
| `promote` | Yes | Yes | Yes | Yes, scoped by task class and authority |
| `conditional_promote` | Yes, scoped | Yes, scoped | Yes, with routing metadata | Yes, scoped |
| `reject` | No | Negative hard-failure credit if evidence strong | Yes for failure/risk predictors | Decrease only with sufficient authority |
| `continue_sampling` | No | Sampling policy only | No final training label | No |
| `shadow_only` | No | Exploration routing only | Shadow dataset only | No solve-time policy update |
| `abstain` | No | Validation-debt routing only | No label | No |
| `quarantine` | No | Hard safety/tamper penalty | Security/tamper dataset only | No positive update |

Component credit must be lineage-aware. If a child changed topology, reviewer loops, executor temperature, retrieval policy, and tool retry limit at once, Agintor may not credit all knobs because the child won. It needs mutation logs, counterfactual reverts, ablation trials, and paired evaluation.

Credit update example:

```json
{
  "decision": "conditional_promote",
  "scope": {
    "task_class": "repo_patch",
    "adapter_kind": "repo_patch",
    "authority_floor": "V6",
    "budget_regime": "medium"
  },
  "component_credit": [
    {
      "component": "state_auditor_agent",
      "effect_interval": [0.041, 0.092],
      "authority": "V6",
      "action": "increase_prior_for_repo_patch_and_service_task"
    },
    {
      "component": "extra_review_loop",
      "effect_interval": [-0.012, 0.019],
      "authority": "V3",
      "action": "do_not_generalize"
    }
  ],
  "forbidden_generalizations": [
    "semantic_open_tasks",
    "low_latency_tasks",
    "tasks_without_state_readback"
  ]
}
```

## 11. Integration with Current Agintor Surfaces

Current Agintor already has several pieces that should be preserved:

| Existing surface | Current role | Required change |
| --- | --- | --- |
| `agintor/contracts/factory.py::BenchmarkPlan` | Frozen task-ID lists | Evolve into or sit beside plan-scoped partition entries with claim refs, verifier refs, fixtures, contamination, and authority floors |
| `agintor/contracts/benchmarks.py::VerifierSpec` | Basic verifier type, artifact contract, tolerance, trace flag | Add claim scope, authority ceiling, health requirements, independence group, execution mode, sealing, leakage risk, and error model |
| `agintor/evaluation/scoring.py::ScoreCalculator` | Converts `RunResult.verifier_score`, cost, latency, faults into task/suite scores | Consume evidence ledgers and authority-aware projections rather than raw verifier scores |
| `agintor/evaluation/evaluator.py::RuntimeEvaluator` | Staged parent/child evaluation through `RuntimeHost` | Freeze validation contract, run validator health, emit ledgers, compare ledgers, and distinguish promote/reject/abstain/shadow/quarantine |
| `agintor/search/engine.py::EvolutionEngine` | Inserts stage-4 children into archive and trains predictors from verifier success | Update archive, scheduler, and predictors only from `OptimizerUpdate`, not from stage-4 score existence |
| `agintor/learning/observations.py` | Extracts predictor observations from traces and verifier success | Attach authority, coverage, ledger refs, and label validity; do not train predictors on shadow or abstained evidence as positive labels |
| `validation_history.json` and `stage_failures.json` | Search reporting artifacts | Become ledger indexes with failure authority, claim coverage, leakage status, and abstention reasons |

New contract models should be added under `agintor/contracts/`:

```python
AuthorityLevel = Literal["V0", "V1", "V2", "V3", "V4", "V5", "V6", "V7"]

class ValidationContract(BaseModel): ...
class ClaimGraph(BaseModel): ...
class Claim(BaseModel): ...
class ProofObligation(BaseModel): ...
class ValidatorHealthReport(BaseModel): ...
class EvidenceLedger(BaseModel): ...
class ComparisonRecord(BaseModel): ...
class OptimizerUpdate(BaseModel): ...
```

`BenchmarkPartitionEntry` should carry:

```python
BenchmarkPartitionEntry = {
    "task_id": "...",
    "partition": "train | proxy | val | test",
    "family": "top | mem | tool | e2e",
    "adapter_kind": "structured_ops | repo_patch | service_task | browser_task | multimodal_task",
    "fixture_ids": ["..."],
    "environment_digest": "...",
    "claim_refs": ["claim.behavior_correctness", "claim.no_side_effects"],
    "verifier_ids": ["..."],
    "authority_floor": "V5",
    "promotion_eligibility": "strong | local | shadow | none",
    "contamination_flags": [],
    "source_task_id": "...",
    "template_id": "...",
    "goal_criteria_targets": ["..."],
}
```

`RunResult` should not be forced to carry all evidence inline. It can keep scalar compatibility fields, but serious evaluation needs refs:

```python
RunResultEvidenceRefs = {
    "evidence_ledger_ref": "...",
    "claim_manifest_ref": "...",
    "validator_report_refs": ["..."],
    "state_diff_refs": ["..."],
    "hard_gate_status": "pass | fail | suspect | quarantine",
}
```

`ScoreCalculator.utility` should become:

```python
def utility_from_ledger(ledger, scoring_policy):
    if ledger.audit_status == "quarantine":
        return UtilityProjection(promotable=False, hard_fail=True)
    if ledger.critical_authority_floor_missed:
        return UtilityProjection(promotable=False, decision="abstain")
    return project_claim_posteriors_to_interval(ledger, scoring_policy)
```

Then `mean_improvement` becomes a comparison over utility intervals and reliability weights, not a plain average of scalar scores.

## 12. Evaluation Stages Reframed

The current staged evaluator is structurally useful, but each stage needs authority semantics.

| Stage | Current intent | Revised validation meaning |
| --- | --- | --- |
| Stage 0 | Patch integrity | Genome admissibility, mutable-boundary proof, no policy/test tampering, no evaluator-surface modification |
| Stage 1 | Deterministic smoke | Reproducibility, nonvacuity smoke, trace stability, runtime entrypoint validity |
| Stage 2 | Proxy | Cheap exploration signal only unless proxy validators meet authority and health floors |
| Stage 3 | Local subset | Underpowered paired evidence; may reject regressions, rarely promote |
| Stage 4 | Full train | Main train-distribution evidence; can update archive only if authority/coverage gates pass |
| Stage 5 | Validation or held-out | Sealed generalization check; required for exported leader claims and strong architecture promotion |
| Health stage | Not explicit enough today | Validator positive/negative controls, mutation kill, leakage, calibration, neutrality, and adversarial tests |

Stage failures must record:

```text
stage
decision
claim_ids
effective_authority
validator_health
coverage
leakage_status
failure_action
rerun_eligibility
redacted_counterexample_ref
```

This lets a failed run teach Agintor why it failed without handing future mutations the hidden answer.

## 13. Serious Task Lanes

`structured_ops` is useful for contract and runtime smoke, but it is too narrow to justify broad MAS claims. The first serious proof lanes should be `repo_patch` and `service_task`.

### Repo Patch

Strong validators:

| Validator | Authority |
| --- | --- |
| Patch applicability and mutable-boundary checks | V3/V5 |
| Public tests | V5 |
| Hidden tests | V6 |
| Property tests | V5 |
| Mutation tests against likely bugs | V5, strong negative evidence |
| Differential checks against reference implementation | V5/V6 |
| Static/type/lint checks | V5 for formalized properties |
| No test/scorer/evaluator tampering audit | V6 hard gate |
| Cost and latency normalization | Process constraint |

Correct Agintor learning:

```text
The child genome's test-generator plus reviewer subgraph improved hidden-test
and mutation-test success under equal budget on repo_patch tasks. Promote that
component for repo_patch tasks with executable tests. Do not generalize to
semantic strategy writing.
```

### Service Task

Strong validators:

| Validator | Authority |
| --- | --- |
| Initial fixture snapshot | V3/V6 |
| Final API/state readback | V6 |
| Allowed transition checker | V5/V6 |
| Idempotence check | V5 |
| Duplicate side-effect detection | V6 hard gate |
| Recipient/entity identity check | V6 |
| Time-zone or unit assertion | V6 |
| Rollback/failure-path simulation | V5/V6 |

Correct Agintor learning:

```text
The state-auditor and rollback-manager subgraph reduced unrelated side effects
and duplicate actions. Promote only for stateful workflows where readback and
side-effect diffs are available.
```

### Open Semantic Task

Open semantic tasks are partially validatable:

| Subclaim | Validator |
| --- | --- |
| Factual statements | Official source lookup and source entailment |
| Numeric claims | Recalculation, spreadsheet check, unit test |
| Internal consistency | Constraint and contradiction checks |
| Required structure | Schema/rubric coverage |
| Persuasiveness or taste | Calibrated preference model or human review only |
| Future success | Usually unverifiable until outcome feedback exists |

Agintor may learn that a source-verifier subgraph reduces unsupported claims. It may not learn that a strategy is "better" solely because a judge preferred it.

## 14. Failure Modes and Mitigations

| Failure mode | What happens | Required mitigation |
| --- | --- | --- |
| Proxy reward hacking | Candidate optimizes verifier artifact rather than task | Sealed eval, adversarial validator tests, authority caps |
| Validator leakage | Candidate sees hidden tests, seeds, rubrics, or reference outputs | Zone separation, canaries, retired cases, quarantine |
| Weak evidence laundering | Many critiques become fake certainty | Authority caps and independence groups |
| Architecture theatre | More agents, longer debate, or richer traces look better without outcome gains | Architecture-neutral validators and outcome dominance |
| Cost confounding | Larger MAS wins by spending much more | Cost-normalized deltas and task-class budget regimes |
| Benchmark overfitting | Search memorizes fixed tasks or verifier shape | Held-out tasks, contamination flags, randomized variants |
| Validator self-dealing | Candidate writes a validator it can pass | Independent health audit before scoring |
| Optional stopping | Repeated looks create false promotions | Fixed-confidence or anytime-valid promotion thresholds |
| Contradictory evidence | Some validators pass and others fail | Increase uncertainty; high-authority contradictions dominate |
| Factual staleness | Old source validates outdated claim | Timestamped provenance and freshness checks |
| Tool side effects | Goal succeeds while damaging unrelated state | Pre/post diff, allowed transition checks, hard side-effect gates |
| Process deception | Trace looks careful but artifact is wrong | Process evidence cannot override outcome evidence |
| Collusive subagents | Solver/critic/judge share hidden state or incentives | Role isolation, hidden-eval invisibility, independent evaluator |
| Search-signal starvation | Predictor and archive complexity outruns evidence volume | `signal_sufficiency.json` gates predictor-backed control |

## 15. Non-Negotiable Invariants

1. Claim locality: no validator certifies claims outside its declared scope.
2. Authority ceiling: health and calibration can lower authority; they cannot raise it.
3. Sealed separation: solver, evaluator, and optimizer must not share hidden-eval secrets.
4. Evidence downstream: rewards are projections of evidence ledgers, not primitive facts.
5. Weak-signal cap: critiques, debate, judges, and consensus cannot become ground truth by accumulation.
6. Abstention: insufficient authority is a first-class outcome, not an error path.
7. Paired comparison: architecture updates require parent/child comparison under matched conditions.
8. Cost normalization: quality gains must be interpreted under tokens, latency, tools, side effects, and risk.
9. Task-class scoping: promotion is never global unless the held-out evidence supports globality.
10. Hidden-case retirement: any hidden case exposed to mutation or debugging is removed from future training signal.
11. Non-self-dealing: generated validators cannot score their own generating run.
12. Provenance completeness: every optimizer update cites contract, genome, ledger, verifier, fixture, and held-out refs.

## 16. WS4 Reframed

The current WS4 plan is directionally right on frozen `BenchmarkPlan`, `VerifierBundle`, typed adapters, provenance, contamination control, resumable search, held-out reporting, and signal sufficiency. Its gap is that it still treats validation as a verifier/reporting surface rather than the authority-bearing interface between Agintor's optimizer and reality.

WS4 should be reframed as:

```text
Benchmark selection + verifier execution + search persistence
under a claim-scoped, authority-aware validation contract.
```

Implementation priority:

1. Add `ValidationContract`, `ClaimGraph`, `EvidenceLedger`, `ComparisonRecord`, and `OptimizerUpdate` schemas.
2. Extend `VerifierSpec` into a health-audited, claim-scoped, authority-capped validator contract.
3. Add plan-scoped `BenchmarkPartitionEntry` records with claim refs, fixture refs, verifier refs, contamination, and authority floors.
4. Make `ScoreCalculator` project from ledgers into intervals instead of reading `RunResult.verifier_score` as reward.
5. Make `RuntimeEvaluator` emit promotion decisions, abstentions, shadow signals, and quarantines.
6. Make `EvolutionEngine` update archive, scheduler, and predictors only through `OptimizerUpdate`.
7. Build serious `repo_patch` and `service_task` lanes with health-tested validators before claiming autonomous improvement.
8. Add `signal_sufficiency.json` as a hard gate for predictor-backed runtime-control expansion.

The final Agintor training signal is not:

```json
{"reward": 0.91}
```

It is:

```json
{
  "decision": "promote",
  "scope": {
    "task_class": "repo_patch",
    "authority_floor": "V6",
    "budget_regime": "medium"
  },
  "parent_genome_hash": "genome.parent",
  "child_genome_hash": "genome.child",
  "validated_delta_interval": [0.044, 0.109],
  "authority_summary": {
    "V6": 0.62,
    "V5": 0.31,
    "V2": 0.00,
    "unverifiable_residual": 0.07
  },
  "hard_gates": {
    "no_test_tampering": "pass",
    "no_hidden_eval_leakage": "pass",
    "no_unrelated_state_change": "pass"
  },
  "component_credit": [
    {
      "component": "property_test_generator_agent",
      "credit": "positive",
      "authority": "V6",
      "effect_interval": [0.035, 0.087]
    }
  ],
  "not_valid_for": [
    "open_ended_semantic_quality",
    "low_latency_chat",
    "tasks_without_executable_oracles"
  ]
}
```

That is the core design: Agintor should learn only from validated effects, scoped to the claims and task classes the evidence can actually support. Everything else is search fuel, diagnostics, or honest abstention.

## References

1. Shengran Hu, Cong Lu, and Jeff Clune. [Automated Design of Agentic Systems](https://arxiv.org/abs/2408.08435). arXiv, 2024/2025.
2. Jiayi Zhang et al. [AFlow: Automating Agentic Workflow Generation](https://arxiv.org/abs/2410.10762). arXiv, 2024/2025.
3. Bo Ma et al. [AutoMaAS: Self-Evolving Multi-Agent Architecture Search for Large Language Models](https://arxiv.org/abs/2510.02669). arXiv, 2025.
4. Yang Liu et al. [MASFactory: A Graph-centric Framework for Orchestrating LLM-Based Multi-Agent Systems with Vibe Graphing](https://arxiv.org/abs/2603.06007). arXiv, 2026.
5. Yulin Peng et al. [SAGE: Multi-Agent Self-Evolution for LLM Reasoning](https://arxiv.org/abs/2603.15255). arXiv, 2026.
6. Jiahao Qiu et al. [Alita-G: Self-Evolving Generative Agent for Agent Generation](https://arxiv.org/abs/2510.23601). arXiv, 2025.
7. Jonathan Noether, Adish Singla, and Goran Radanovic. [MaMa: A Game-Theoretic Approach for Designing Safe Agentic Systems](https://arxiv.org/abs/2602.04431). arXiv, 2026.
8. Xiaohua Wang et al. [Reward Hacking in the Era of Large Models: Mechanisms, Emergent Misalignment, Challenges](https://arxiv.org/abs/2604.13602). arXiv, 2026.
9. Earl T. Barr et al. [The Oracle Problem in Software Testing: A Survey](https://discovery.ucl.ac.uk/id/eprint/1471263/). IEEE Transactions on Software Engineering, 2015.
10. Tsong Yueh Chen et al. [Metamorphic Testing: A Review of Challenges and Opportunities](https://i.cs.hku.hk/~tse/Papers/2010s/hlmtCSUR.html). ACM Computing Surveys, 2018.
11. Lianmin Zheng et al. [Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685). NeurIPS Datasets and Benchmarks, 2023.
12. Yang Liu et al. [G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment](https://aclanthology.org/2023.emnlp-main.153/). EMNLP, 2023.
13. Qian Wang and Ralf Jung. [Rustlantis: Randomized Differential Testing of the Rust Compiler](https://2024.splashcon.org/details/splash-2024-oopsla/123/Rustlantis-Randomized-Differential-Testing-of-the-Rust-Compiler). OOPSLA, 2024.
14. Kyoungseok Jang, Junpei Komiyama, and Kazutoshi Yamazaki. [Fixed Confidence Best Arm Identification in the Bayesian Setting](https://arxiv.org/abs/2402.10429). arXiv, 2024.