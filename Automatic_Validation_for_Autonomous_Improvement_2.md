That changes the framing in an important way.

For **Agintor**, the validation problem is not:

> “Was this LLM answer better?”

It is:

> “Did this MAS architecture, template choice, coordination protocol, and hyperparameter mutation produce a real, repeatable improvement for this task class under sealed validation?”

So the object being optimized is not a completion. It is a **MAS genome**.

A useful mental model is close to graph-based MAS orchestration: agents/sub-workflows are nodes, dependencies and message passing are edges, and a natural-language intent is compiled into an executable workflow graph. Recent MASFactory work frames LLM-based MAS workflows this way, with reusable graph components, topology preview, runtime tracing, and natural-language-to-workflow compilation. ([arXiv][1])

For Agintor, validation must happen at four layers:

```text
1. Outcome validity:
   Did the produced artifact/action solve the user task?

2. Process validity:
   Did the MAS use tools, memory, state, and communication safely?

3. Architecture validity:
   Did this template/topology/hyperparameter choice cause improvement?

4. Search-validity:
   Is Agintor learning a generalizable lesson, or overfitting one evaluator/task?
```

Agent evaluation is already harder than ordinary unit testing because it includes behavior, capabilities, reliability, safety, tool use, memory, and long-horizon interaction, not merely final-answer correctness. ([sap-samples.github.io][2]) Agintor adds another layer: it must validate **the factory’s design decision**, not just the agent run.

---

# 1. Corrected unit of optimization

Agintor should optimize this object:

```python
MASGenome = {
    "template_id": "planner_executor_reviewer",
    "graph": {
        "nodes": [
            "planner",
            "executor",
            "critic",
            "tool_auditor",
            "final_synthesizer"
        ],
        "edges": [
            ("planner", "executor"),
            ("executor", "critic"),
            ("critic", "executor"),
            ("executor", "tool_auditor"),
            ("critic", "final_synthesizer")
        ]
    },
    "agent_specs": {
        "planner": {
            "role_prompt_hash": "...",
            "model": "model_A",
            "temperature": 0.2,
            "tools": []
        },
        "executor": {
            "role_prompt_hash": "...",
            "model": "model_B",
            "temperature": 0.1,
            "tools": ["code_runner", "browser", "db_api"]
        },
        "critic": {
            "role_prompt_hash": "...",
            "model": "model_C",
            "temperature": 0.0,
            "tools": ["validator_suggestor"]
        }
    },
    "coordination_policy": {
        "handoff_protocol": "structured_claims",
        "max_review_loops": 2,
        "voting_rule": "critic_gate_then_synthesizer",
        "termination_rule": "all_hard_claims_checked_or_abstain"
    },
    "memory_policy": {
        "shared_memory": True,
        "private_scratchpads": True,
        "retrieval_scope": "task_local_only"
    },
    "tool_policy": {
        "side_effect_mode": "sandbox_until_commit",
        "requires_pre_action_plan": True,
        "requires_post_action_diff": True
    },
    "budget": {
        "max_tokens": 120000,
        "max_tool_calls": 80,
        "max_wall_time_s": 600
    }
}
```

The optimizer should not learn:

```text
“Run 17 got a higher score.”
```

It should learn:

```text
“For software-debugging tasks with hidden executable validation,
adding an independent tester/reviewer subgraph improved validated
bug-fix success by +8.4% under equal budget, with no safety regression,
but only for medium-complexity repositories.”
```

That distinction is the whole system.

---

# 2. Agintor validation architecture

```text
                         ┌──────────────────────┐
                         │ User-defined task     │
                         └──────────┬───────────┘
                                    ▼
                         ┌──────────────────────┐
                         │ Task Envelope         │
                         │ domain, artifacts,    │
                         │ tools, risks, claims  │
                         └──────────┬───────────┘
                                    ▼
                         ┌──────────────────────┐
                         │ Eval Recipe Compiler  │
                         │ validators, authority,│
                         │ abstention conditions │
                         └──────────┬───────────┘
                                    ▼
        ┌────────────────────────────────────────────────────┐
        │ Agintor Search Layer                                │
        │ chooses template + topology + MAS hyperparameters   │
        └──────────┬─────────────────────────────────────────┘
                   ▼
        ┌────────────────────────────────────────────────────┐
        │ MAS Runner                                          │
        │ executes candidate genome on task/simulation suite  │
        └──────────┬─────────────────────────────────────────┘
                   ▼
        ┌────────────────────────────────────────────────────┐
        │ Sealed Evaluator                                    │
        │ hidden tests, state checks, source checks, judges   │
        └──────────┬─────────────────────────────────────────┘
                   ▼
        ┌────────────────────────────────────────────────────┐
        │ Evidence Ledger                                     │
        │ claim validity, process validity, cost, uncertainty │
        └──────────┬─────────────────────────────────────────┘
                   ▼
        ┌────────────────────────────────────────────────────┐
        │ Architecture Comparator                             │
        │ parent/child or population comparison under risk    │
        └──────────┬─────────────────────────────────────────┘
                   ▼
        ┌────────────────────────────────────────────────────┐
        │ Agintor Optimizer Update                            │
        │ template prior, hyperparameter prior, mutation rule  │
        └────────────────────────────────────────────────────┘
```

Agintor should maintain **beliefs over architecture families**, not just scores.

Example:

```python
ArchitectureBelief = {
    "task_class": "software_debugging",
    "template_effects": {
        "single_agent": {
            "mean_validated_utility": 0.42,
            "uncertainty": 0.09
        },
        "planner_executor": {
            "mean_validated_utility": 0.51,
            "uncertainty": 0.08
        },
        "planner_executor_reviewer": {
            "mean_validated_utility": 0.59,
            "uncertainty": 0.05
        },
        "debate_committee": {
            "mean_validated_utility": 0.47,
            "uncertainty": 0.12
        }
    },
    "known_regressions": {
        "debate_committee": ["high_token_cost", "slow_on_simple_tasks"],
        "planner_executor_reviewer": ["overhead_on_tiny_tasks"]
    }
}
```

---

# 3. MAS-specific validation authority levels

The earlier authority taxonomy still applies, but for Agintor it should be interpreted at the **MAS design layer**.

|  Level | Agintor interpretation                                                                                                     | May update optimizer?                      |
| -----: | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| **M0** | Core task objective is unverifiable. Example: “make the most brilliant strategy.”                                          | No architecture promotion.                 |
| **M1** | MAS trace looks plausible. Agents debated, reflected, planned.                                                             | No. Diagnostic only.                       |
| **M2** | LLM judge prefers one MAS output.                                                                                          | Weak exploration signal only.              |
| **M3** | MAS followed protocol: valid schemas, valid tool calls, no obvious hallucinated tool results.                              | Update process priors only.                |
| **M4** | Executable validators partially check outcome: tests, assertions, source checks, state diffs.                              | Local update for checked claims.           |
| **M5** | Sealed independent oracle: hidden tests, simulator, private task variants, reference output, authoritative state readback. | Strong architecture update allowed.        |
| **M6** | Formal proof or certified contract: orchestration invariant, safety property, type/proof checker.                          | Strongest, but only for formalized claims. |

The key rule:

```text
Agintor may only promote a MAS genome to the extent that the task objective
was validated by high-authority evidence.
```

A beautiful multi-agent debate that wins an LLM judge but has no ground truth should not cause global template promotion.

---

# 4. Goal-to-eval compiler for arbitrary user tasks

For every user task, Agintor should compile a **Task Envelope** before choosing or evolving a MAS.

```python
TaskEnvelope = {
    "task_id": "...",
    "natural_goal": "...",
    "domain": "software | factual | stateful_workflow | semantic_open_ended | mixed",
    "expected_artifact": "code_patch | answer | database_state | email | memo | plan | other",
    "allowed_tools": [...],
    "forbidden_actions": [...],
    "stateful": True,
    "external_side_effect_risk": "low | medium | high",
    "validation_profile": {
        "strongly_validatable_claims": [...],
        "partially_validatable_claims": [...],
        "unverifiable_claims": [...]
    },
    "minimum_authority_for_promotion": "M4 | M5 | M6",
    "abstention_policy": {...}
}
```

Then compile the task into four claim classes.

```text
A. Outcome claims
   Did the final artifact/action satisfy the request?

B. State claims
   Did the world/tool/database/calendar/email/filesystem end in the right state?

C. Process claims
   Did the MAS follow safe protocols, tool constraints, and cost limits?

D. Architecture claims
   Did this MAS configuration outperform another under matched conditions?
```

For open-ended user tasks, Agintor should force the MAS to produce a **claim manifest**:

```python
ClaimManifest = {
    "final_answer": "...",
    "atomic_claims": [
        {
            "claim": "The deadline is June 15, 2026.",
            "type": "factual",
            "source_required": True,
            "validator": "official_source_check"
        },
        {
            "claim": "This is the best strategy for the company.",
            "type": "strategic_judgment",
            "source_required": False,
            "validator": None,
            "status": "unverifiable_without_human_or_market_feedback"
        }
    ],
    "assumptions": [...],
    "tool_actions_taken": [...],
    "unverified_residuals": [...]
}
```

This is crucial. Agintor should not let a MAS emit an opaque final answer and then ask a judge, “Was it good?” The MAS must expose the claims it wants credit for.

---

# 5. Template cards

Each original MAS template should have a **template card** describing when it is eligible.

```python
TemplateCard = {
    "template_id": "planner_executor_reviewer",
    "description": "Planner decomposes task, executor acts, reviewer checks, executor revises.",
    "best_for": [
        "software_debugging",
        "tool_workflows",
        "multi-step factual synthesis"
    ],
    "bad_for": [
        "tiny low-latency tasks",
        "pure creative writing under weak validation"
    ],
    "required_validators": [
        "outcome_validator",
        "tool_trace_validator",
        "cost_validator"
    ],
    "known_failure_modes": [
        "reviewer theatre",
        "looping critiques",
        "over-planning",
        "critic blocks valid unconventional solutions"
    ],
    "mutable_hyperparameters": {
        "max_review_loops": [0, 1, 2, 3],
        "critic_threshold": [0.5, 0.7, 0.9],
        "planner_detail_level": ["low", "medium", "high"],
        "executor_temperature": [0.0, 0.1, 0.3],
        "tool_retry_limit": [0, 1, 2]
    },
    "protected_invariants": [
        "reviewer_cannot_access_hidden_eval",
        "executor_cannot_modify_validators",
        "tool_auditor_must_run_on_stateful_tasks"
    ]
}
```

Agintor’s search should not treat templates as arbitrary strings. They are typed, constrained, risk-scored design patterns.

---

# 6. Validator-health system for MAS factories

For ordinary task validation, validator health checks whether a test or judge is reliable.

For Agintor, validator health must additionally ask:

```text
Does this validator accidentally prefer one MAS architecture style?
```

For example:

* A judge may prefer verbose debate traces.
* A rubric may reward plans even when plans do not improve outcomes.
* A stateful workflow evaluator may ignore duplicate side effects.
* A coding benchmark may reward hidden-test overfitting.
* A tool-use validator may reward “using more tools” rather than using the right tool.

So Agintor needs **validator neutrality tests**.

```python
ValidatorHealth = {
    "nonvacuity": 0.93,
    "negative_control_fail_rate": 0.89,
    "positive_control_pass_rate": 0.91,
    "mutation_kill_rate": 0.84,
    "task_coverage": 0.77,
    "process_coverage": 0.69,
    "architecture_neutrality": 0.81,
    "cost_fairness": 0.88,
    "leakage_resistance": 0.96,
    "anti_theatre_score": 0.74,
    "flake_rate": 0.03,
    "effective_authority": "M5"
}
```

Important MAS-specific health checks:

| Health check                         | Meaning                                                                                         |
| ------------------------------------ | ----------------------------------------------------------------------------------------------- |
| **Architecture neutrality**          | Does the validator reward outcomes rather than debate/planning style?                           |
| **Cost fairness**                    | Are high-token MAS templates penalized or budget-normalized?                                    |
| **Topology invariance**              | Could a different valid topology pass equally?                                                  |
| **Anti-theatre score**               | Does the validator distinguish real verification from fake critique/reflection?                 |
| **Tool-grounding sensitivity**       | Does it catch hallucinated tool outputs or missing readbacks?                                   |
| **Coordination-failure sensitivity** | Does it catch role collapse, infinite loops, dead agents, duplicate actions?                    |
| **Leakage resistance**               | Can any agent see hidden tests, judge prompts, canaries, reference outputs, or evaluator files? |

---

# 7. Sealed evaluation protocol for Agintor

Agintor needs stricter sealing than a single-agent system because there are more surfaces for leakage.

```text
┌────────────────────┐
│ Agintor Search Box │
│ chooses genome     │
│ sees public task   │
│ sees prior ledgers │
└─────────┬──────────┘
          │ frozen MAS genome hash
          ▼
┌────────────────────┐
│ MAS Runner Box      │
│ executes agents     │
│ no hidden eval       │
│ no validator access  │
└─────────┬──────────┘
          │ artifact + trace digest
          ▼
┌────────────────────┐
│ Eval Box            │
│ hidden tests        │
│ sealed judge prompts│
│ state assertions    │
│ reference oracles   │
└─────────┬──────────┘
          │ sanitized evidence ledger
          ▼
┌────────────────────┐
│ Optimizer Box       │
│ updates Agintor     │
│ never sees secrets  │
└────────────────────┘
```

Protocol rules:

```text
1. Freeze MAS genome before evaluation.
   Template, prompts, topology, tools, model choices, budgets, and memory policy are hashed.

2. Generate or reveal hidden eval seeds only after genome commit.

3. Hide validator prompts, hidden tests, reference outputs, and canaries from all MAS agents.

4. The MAS may propose validators, but those validators are not trusted until audited independently.

5. Optimizer receives sanitized claim-level ledgers, not hidden tests or raw judge prompts.

6. If a hidden test is exposed for debugging, retire it from future training signal.

7. If a candidate touches evaluator state, modifies tests, accesses canaries, or tampers with logs, quarantine rather than merely penalize.
```

---

# 8. MAS comparison algebra

Agintor should compare genomes, not isolated outputs.

Let:

```text
G_p = parent MAS genome
G_c = child MAS genome
T   = task distribution inferred from the user-defined task
E   = sealed evaluation recipe
```

Each genome gets a utility interval:

[
U(G, T, E) =
\text{validated outcome}

* \lambda \cdot \text{cost}
* \rho \cdot \text{risk}
* \eta \cdot \text{unverifiable mass}
  ]

But this should be interval-valued:

[
U(G) \in [U^-(G), U^+(G)]
]

Compare child to parent pessimistically:

[
\Delta^- = U^-(G_c) - U^+(G_p)
]

[
\Delta^+ = U^+(G_c) - U^-(G_p)
]

Decision:

| Condition                                            | Agintor decision                                               |
| ---------------------------------------------------- | -------------------------------------------------------------- |
| Child violates hard safety or tool invariant         | Reject or quarantine                                           |
| Child has hidden-eval leakage                        | Quarantine                                                     |
| (\Delta^- > \delta) under strong validators          | Promote                                                        |
| (\Delta^+ < -\delta)                                 | Reject                                                         |
| Interval overlaps threshold                          | Abstain                                                        |
| Only weak judge/process evidence exists              | Shadow update only                                             |
| Improvement exists only by spending much more budget | Do not promote unless user task class values quality over cost |
| Improvement on one slice but regression on another   | Conditional promotion or template specialization               |

The result is not “better MAS globally.” It is:

```text
This genome is better for this task class under this validation authority and budget regime.
```

---

# 9. Statistical promotion rule

Agintor should use **paired evaluations**.

For each task instance (t_i):

```text
Run parent genome Gp on t_i.
Run child genome Gc on same t_i.
Use same public information and same budget.
Evaluate both with sealed validators.
Compute validated delta d_i.
```

Then promote only if:

```python
def promote_child(parent, child, paired_results):
    if child.has_eval_tampering:
        return "quarantine"

    if child.has_hard_safety_failure:
        return "reject"

    if paired_results.validator_authority_lcb < REQUIRED_AUTHORITY:
        return "abstain"

    if paired_results.unverifiable_mass > MAX_UNVERIFIABLE_MASS:
        return "abstain"

    if paired_results.cost_normalized_delta_lcb > MIN_MEANINGFUL_IMPROVEMENT:
        return "promote"

    if paired_results.delta_ucb < -MIN_MEANINGFUL_REGRESSION:
        return "reject"

    return "abstain"
```

The important bit is **cost-normalized delta**.

A 5-agent architecture that improves quality by 2% while using 10x tokens may not be better. Agintor should learn different policies for:

```text
low-latency tasks
high-stakes tasks
cheap exploratory tasks
stateful tool tasks
software tasks
semantic writing tasks
```

---

# 10. Credit assignment across MAS hyperparameters

Agintor should not mutate five things and then blindly credit the whole genome.

Bad update:

```text
Child won.
Therefore planner + reviewer + higher temperature + more tool retries + larger context are all good.
```

Good update:

```text
Child won on software tasks.
Mutation log:
  + added reviewer
  + increased review loops from 1 to 2
  + lowered executor temperature
  + added property-test tool

Attribution:
  reviewer addition: likely positive
  property-test tool: strongly positive
  review loops 2: uncertain
  lower temperature: weak positive
  higher total budget: confounded
```

Use a lineage graph.

```python
GenomeMutation = {
    "parent_genome": "G_102",
    "child_genome": "G_117",
    "mutations": [
        {
            "knob": "add_agent",
            "value": "property_test_generator",
            "risk": "medium",
            "expected_effect": "better software validation"
        },
        {
            "knob": "max_review_loops",
            "old": 1,
            "new": 2,
            "risk": "low"
        }
    ]
}
```

Then update beliefs at three levels:

```python
ArchitectureSignal = {
    "decision": "promote",
    "scope": {
        "task_class": "software_debugging",
        "artifact_type": "code_patch",
        "validation_authority": "M5",
        "budget_regime": "medium"
    },
    "genome_delta": {
        "parent": "G_102",
        "child": "G_117",
        "validated_delta_interval": [0.061, 0.113]
    },
    "component_credit": [
        {
            "component": "property_test_generator_agent",
            "effect_interval": [0.035, 0.087],
            "confidence": 0.88,
            "authority": "M5"
        },
        {
            "component": "max_review_loops=2",
            "effect_interval": [-0.011, 0.026],
            "confidence": 0.42,
            "authority": "M4",
            "action": "do_not_generalize"
        }
    ],
    "negative_findings": [
        {
            "component": "extra_review_loop",
            "finding": "higher latency on simple tasks"
        }
    ]
}
```

Agintor’s optimizer should update:

```text
template priors
knob priors
task-class routing policy
budget policy
tool-policy priors
risk priors
```

not merely a global scalar reward.

---

# 11. Weak signal policy inside Agintor

MAS systems naturally produce lots of weak evidence:

```text
planner confidence
critic approval
debate winner
self-reflection
chain-of-thought consistency
committee vote
LLM judge score
preference model score
```

These are useful, but dangerous.

For Agintor:

| Signal                              | Allowed use                                    | Forbidden use               |
| ----------------------------------- | ---------------------------------------------- | --------------------------- |
| Internal critic says output is good | Diagnose, trigger revision                     | Promote architecture        |
| Debate winner                       | Candidate selection                            | Ground-truth reward         |
| LLM judge                           | Triage, semantic weak signal                   | Override tests/source/state |
| Process trace                       | Detect loops, tool misuse, suspicious behavior | Prove correctness           |
| Self-reported confidence            | Calibration feature                            | Reward                      |
| Agent consensus                     | Uncertainty estimate                           | Truth oracle                |
| Preference model                    | Soft product signal                            | High-stakes promotion       |

A judge can be part of the system, but it should be treated as a **weak validator** unless calibrated against trusted labels or stronger outcome evidence. Practical agent-evaluation guidance also distinguishes ground-truth checks, LLM-as-judge methods, and human review, noting that LLM judges scale well but inherit model biases and need calibration against higher-quality review. ([Google Cloud][3])

---

# 12. Evolution modes by validation strength

Agintor should have different autonomy modes.

| Mode                      | Validation available | Allowed evolution                                                                 |
| ------------------------- | -------------------- | --------------------------------------------------------------------------------- |
| **Certified mode**        | M5/M6 evidence       | Promote templates and hyperparameters automatically                               |
| **Executable mode**       | M4 evidence          | Local task-class updates; require extra regression checks                         |
| **Grounded-partial mode** | M3/M4 evidence       | Update only checked subskills: formatting, citation use, tool-call correctness    |
| **Weak-semantic mode**    | M2 evidence          | Shadow learning only; use for proposal generation, not promotion                  |
| **Unverifiable mode**     | M0/M1 only           | No optimizer update; ask for instrumentation, human feedback, or measurable proxy |

This prevents Agintor from “evolving” on vibes.

---

# 13. Examples

## A. Software task

User task:

```text
Fix this bug in a repo.
```

Agintor candidates:

```text
G1: single coding agent
G2: planner + coder
G3: planner + coder + reviewer
G4: planner + coder + test-generator + reviewer
```

Strong validators:

```text
hidden tests
public tests
mutation tests
property tests
static checks
no-test-tampering audit
cost and latency
```

Correct learning signal:

```text
G4 beats G3 on hidden tests and mutation tests under same budget.
Property-test-generator agent gets positive credit.
Reviewer loop depth remains uncertain.
Promote G4 only for software-debugging tasks, not globally.
```

Bad learning signal:

```text
G4 produced longer reasoning and the critic sounded more confident.
Promote G4.
```

That is fake progress.

---

## B. Factual task

User task:

```text
Find the current compliance deadline and summarize exceptions.
```

Candidate MAS templates:

```text
G1: answer-only agent
G2: researcher + writer
G3: researcher + source-verifier + writer
G4: researcher + contradiction-checker + source-verifier + writer
```

Validators:

```text
official source retrieval
atomic claim extraction
citation-span support
date freshness check
contradiction check
unsupported-claim detector
```

Correct learning:

```text
Source-verifier subgraph reduces unsupported claims.
Contradiction-checker helps when sources conflict.
Promote G4 for factual/compliance tasks when source authority is available.
```

Abstention case:

```text
Sources conflict, no authoritative source found.
Do not decide which MAS is better on final answer correctness.
Reward only explicit uncertainty and source-grounded subclaims.
```

---

## C. Stateful tool workflow

User task:

```text
Schedule a meeting, invite the right people, and send the agenda.
```

Candidate MAS templates:

```text
G1: direct executor
G2: planner + executor
G3: planner + executor + state auditor
G4: planner + executor + state auditor + rollback manager
```

Validators:

```text
calendar API readback
email sent-log check
pre/post state diff
idempotence test
recipient identity check
time-zone assertion
no unrelated side effects
```

Correct learning:

```text
State-auditor agent reduces side effects.
Rollback manager helps when tool calls fail.
Promote G4 for stateful external-tool tasks.
```

Hard rejection:

```text
G4 achieves the meeting goal but sends duplicate emails.
Reject or partial-credit only.
State invariant failed.
```

---

## D. Open-ended semantic task

User task:

```text
Write a compelling market-entry strategy.
```

Candidate MAS templates:

```text
G1: single writer
G2: researcher + writer
G3: researcher + strategist + critic + writer
G4: researcher + financial-model checker + risk critic + writer
```

Validators:

```text
structure check
source support for factual claims
financial calculation checks
risk-category coverage
internal consistency checks
LLM judge for clarity/persuasiveness
```

Correct learning:

```text
G4 improves factual grounding and financial consistency.
Promote the financial-checker subgraph for strategy tasks with numeric claims.
Do not claim G4 is "more strategically correct" unless expert/user/market feedback exists.
```

Abstention:

```text
LLM judge prefers G3 as more compelling.
No high-authority evidence supports strategic superiority.
No global promotion.
```

---

# 14. The Agintor-specific invariant

Agintor should never update its MAS-search policy from:

```text
score(candidate_output)
```

It should update only from:

```text
validated_effect(
    child_genome,
    parent_genome,
    task_class,
    sealed_eval_recipe,
    authority_profile,
    cost_profile,
    risk_profile,
    uncertainty_interval
)
```

In compact form:

```python
def agintor_training_signal(parent_genome, child_genome, task_suite):
    parent_ledgers = evaluate_genome(parent_genome, task_suite)
    child_ledgers = evaluate_genome(child_genome, task_suite)

    comparison = compare_under_uncertainty(
        parent_ledgers,
        child_ledgers,
        normalize_cost=True,
        enforce_hard_gates=True,
        account_for_unverifiable_mass=True
    )

    if comparison.leakage_or_tampering:
        return Quarantine(child_genome)

    if comparison.only_weak_evidence:
        return ShadowSignal(comparison)

    if comparison.lower_bound_delta > MIN_MEANINGFUL_IMPROVEMENT:
        return Promote(
            child_genome,
            scope=comparison.task_class_scope,
            component_credit=comparison.component_credit,
            authority=comparison.authority_profile
        )

    if comparison.upper_bound_delta < -MIN_MEANINGFUL_REGRESSION:
        return Reject(child_genome)

    return Abstain(comparison.reason)
```

---

# 15. What Agintor should output to its optimizer

Not:

```json
{
  "reward": 0.91
}
```

But:

```json
{
  "decision": "promote",
  "scope": {
    "task_class": "stateful_tool_workflow",
    "validation_mode": "sealed_state_readback",
    "authority": "M5"
  },
  "parent_genome": "planner_executor_v3",
  "child_genome": "planner_executor_state_auditor_v1",
  "validated_delta": {
    "lower_bound": 0.074,
    "estimate": 0.113,
    "upper_bound": 0.149
  },
  "cost_delta": {
    "token_multiplier": 1.22,
    "wall_time_multiplier": 1.17
  },
  "hard_gates": {
    "no_unrelated_state_changes": "pass",
    "no_duplicate_actions": "pass",
    "no_eval_tampering": "pass"
  },
  "component_credit": [
    {
      "component": "state_auditor_agent",
      "credit": "positive",
      "effect_interval": [0.051, 0.121],
      "authority": "M5"
    },
    {
      "component": "extra_planning_step",
      "credit": "uncertain",
      "effect_interval": [-0.013, 0.027],
      "authority": "M3"
    }
  ],
  "unverifiable": [
    {
      "claim": "user satisfaction with wording of email",
      "reason": "no user feedback available"
    }
  ],
  "optimizer_update": {
    "increase_prior_for": [
      "state_auditor_agent on stateful tasks",
      "post_action_diff validation",
      "idempotence check before final commit"
    ],
    "do_not_generalize": [
      "extra planning depth"
    ]
  }
}
```

---

# Bottom line

For Agintor, the automatic validation problem becomes:

> Build a sealed, claim-level, authority-aware experimental system that can tell whether a MAS genome mutation improved validated task utility for a specific task class, without confusing better-looking traces, stronger self-critique, more agents, more tokens, or judge preference with real progress.

The core design move is:

```text
User task
→ Task Envelope
→ Claim graph
→ Eval recipe
→ Frozen MAS genome
→ Sealed paired evaluation
→ Evidence ledger
→ Architecture-level delta
→ Scoped optimizer update or abstention
```

Agintor should evolve aggressively only where validation is strong, evolve locally where validation is partial, and abstain where the task objective cannot be grounded.

[1]: https://arxiv.org/abs/2603.06007 "[2603.06007] MASFactory: A Graph-centric Framework for Orchestrating LLM-Based Multi-Agent Systems with Vibe Graphing"
[2]: https://sap-samples.github.io/llm-agents-eval-tutorial/ "KDD 2025 Tutorial: Evaluation & Benchmarking of LLM Agents"
[3]: https://cloud.google.com/blog/topics/developers-practitioners/agent-factory-recap-a-deep-dive-into-agent-evaluation-practical-tooling-and-multi-agent-systems "Agent Factory Recap: A Deep Dive into Agent Evaluation, Practical Tooling, and Multi-Agent Systems | Google Cloud Blog"
