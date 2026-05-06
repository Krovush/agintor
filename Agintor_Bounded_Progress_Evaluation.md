# Agintor Bounded Progress Evaluation

## Replacing the “Oracle” Abstraction with Domain Evidence Institutions

**Status:** conceptual redesign supplement for Agintor validated evolution  
**Purpose:** define how Agintor can honestly distinguish **good** results from **better** results in bounded domains  
**Scope:** theory, statistical semantics, generator health, domain-evaluation contracts, and promotion philosophy  
**Deliberate omissions:** implementation plans, schemas, code, module layout, and tactical refactors

---

## Abstract

Calling the evaluator an “Oracle” hides the hard part. The hard part is not comparing two artifacts after they already exist. The hard part is constructing domains in which comparison has epistemic force: challenge generators must produce realistic tasks, private answers must be reliable, generated tasks must not collapse into junk, and repeated evolutionary probing must not convert statistical noise into false progress.

This document replaces the monolithic Progress Oracle with a theory of **Domain Evidence Institutions**. A domain evidence institution is a bounded, auditable evaluation world consisting of a challenge distribution, a reference-answer mechanism, task realism audits, metamorphic relations, validator health tests, held-out templates, statistical promotion rules, and scope restrictions. Agintor may claim capability improvement only inside such a world, and only when a child genome beats its parent on grounded quality axes under an anytime-valid or pre-registered fixed-budget test.

The resulting system is intentionally not general AGI magic. It can make Agintor useful in bounded domains such as repository patches, deterministic tool workflows, stateful service tasks, structured memory retrieval, and generated end-to-end tool/memory tasks. It cannot honestly learn “better taste,” “better strategy,” or “better architecture” unless those words are grounded in observable outcomes, human/user feedback, or formal task environments.

The core invariant is:

$$
\textbf{No capability promotion without a domain evidence contract.}
$$

A cost-only win may produce efficiency progress. A judge-only preference win may produce preference-model progress. Neither is capability progress unless it is backed by grounded domain evidence.

---

## 1. Why the Earlier Oracle Design Was Still Too Magical

The previous design correctly separated capability improvement from latency/cost optimization, but it still smuggled in the impossible object under a friendly name: the **Progress Oracle**. The name implied that, once parent and child artifacts existed, some comparator could decide which was better. That is the wrong center of gravity.

The true object is not a comparator. The true object is an evaluation institution:

$$
\mathfrak{E}_D
=
\Big(
\mathcal{X}_D,
\mathcal{Y}_D,
\mathcal{Q}_D,
\mathcal{A}_D,
\mathcal{V}_D,
\mathcal{M}_D,
\mathcal{R}_D,
\mathcal{H}_D,
\mathcal{S}_D
\Big)
$$

where:

| Symbol | Meaning |
|---|---|
| $D$ | bounded domain, such as repository patching or service-state transition tasks |
| $\mathcal{X}_D$ | task space |
| $\mathcal{Y}_D$ | artifact/output space |
| $\mathcal{Q}_D$ | generated challenge distribution family |
| $\mathcal{A}_D$ | private answer or acceptance-predicate mechanism |
| $\mathcal{V}_D$ | validators and score functions |
| $\mathcal{M}_D$ | metamorphic relations and invariants |
| $\mathcal{R}_D$ | empirical reference distribution of real tasks |
| $\mathcal{H}_D$ | health audits over generators, validators, answers, and realism |
| $\mathcal{S}_D$ | statistical promotion rule |

A comparator is merely one instrument inside $\mathfrak{E}_D$. If $\mathfrak{E}_D$ is weak, the comparator is decoration. If $\mathfrak{E}_D$ is strong, the comparator can be simple.

This is the main correction:

$$
\text{Oracle design} \quad \longrightarrow \quad \text{domain evidence institution design.}
$$

---

## 2. First Principles

### 2.1 Validation is not progress

Validation answers:

$$
\text{Is this evidence trustworthy?}
$$

Progress answers:

$$
\text{Did the child become better than the parent on a grounded quality axis?}
$$

Promotion answers:

$$
\text{What kind of update may the optimizer consume?}
$$

The categories must remain separate:

| Evidence pattern | Allowed conclusion |
|---|---|
| same quality, lower cost | efficiency improvement |
| better hidden challenge performance | capability improvement |
| better human/user preference | preference improvement |
| better LLM-judge score only | weak preference hypothesis |
| better trace aesthetics | no capability update |
| better on generated junk tasks | no real-domain capability update |
| better on generated tasks with unproven realism | generated-domain-only progress |

### 2.2 “Better” is domain-scoped

For a parent genome $G_p$ and child genome $G_c$, define a quality vector:

$$
q_D(x, y)
=
\big(q_{D,1}(x,y),\ldots,q_{D,k}(x,y)\big)
\in [0,1]^k.
$$

The axis-level child-parent effect is:

$$
\Delta_{D,a}(G_c,G_p)
=
\mathbb{E}_{x\sim P_D}
\left[
q_{D,a}\big(x,Y_{G_c}(x)\big)
-
q_{D,a}\big(x,Y_{G_p}(x)\big)
\right].
$$

A capability promotion is legal only if there exists at least one capability axis $a$ such that:

$$
L_a > \epsilon_a
\quad\text{and}\quad
\forall b\in\mathcal{A}_{\mathrm{protected}},\; U_b \ge -\rho_b,
$$

where $[L_a,U_a]$ is an anytime-valid or pre-registered confidence interval for $\Delta_{D,a}$, $\epsilon_a$ is the minimum meaningful improvement, and $\rho_b$ is the tolerated protected-axis regression.

Cost and latency are not included in $q_D$; they live in a separate efficiency vector:

$$
c_D(x,G) = (\mathrm{tokens}, \mathrm{walltime}, \mathrm{toolcalls}, \mathrm{provider\_cost}, \ldots).
$$

This prevents the most common failure:

$$
\text{same artifact quality} + \text{lower cost}
\neq
\text{better capability}.
$$

### 2.3 A capability claim is only as general as its evaluation distribution

If Agintor evaluates on $P_D$, the most it can claim is improvement over $P_D$. It may not silently generalize to “software engineering,” “strategy,” or “architecture.” A valid promotion carries a scope:

$$
\mathrm{scope}(\sigma)
=
(D, P_D, \mathcal{A}_{\mathrm{axes}}, \mathcal{A}_{\mathrm{authority}}, \mathcal{H}_{\mathrm{health}}, \alpha_{\mathrm{spent}}).
$$

A promotion outside that scope requires separate evidence.

---

## 3. The Domain Evidence Contract

A domain $D$ becomes promotion-eligible only if it has a **Domain Evidence Contract**:

$$
\mathcal{C}_D
=
\left(
P_D,
Q_D,
A_D,
V_D,
M_D,
R_D,
H_D,S_D
\right).
$$

### 3.1 Contract obligations

A valid contract must answer eight questions.

| Question | Required answer |
|---|---|
| What tasks are sampled? | a task distribution or mixture of distributions |
| Why are tasks realistic? | reference corpus, slice taxonomy, and realism audit |
| How are private answers known? | reference solver, simulator, acceptance predicate, or metamorphic relation |
| What makes a result better? | axis-level quality functional |
| How is junk generation detected? | generator health suite |
| How is leakage prevented? | private answer isolation and template retirement |
| How is repeated search controlled? | fixed budget or anytime-valid test |
| How far may the conclusion generalize? | explicit promotion scope |

If any answer is missing, the domain is shadow-only or diagnostic-only.

### 3.2 Effective authority of a domain

The authority of a domain evaluation is bounded by its weakest institutional component:

$$
A_{\mathrm{eff}}(\mathcal{C}_D)
=
\min
\Big(
A_{\mathrm{answer}},
A_{\mathrm{validator}},
A_{\mathrm{generator\_health}},
A_{\mathrm{realism}},
A_{\mathrm{statistics}},
A_{\mathrm{leakage}}
\Big).
$$

This rule is deliberately harsh. A perfect hidden answer oracle does not rescue a junk task generator. A beautiful generator does not rescue an unreliable answer mechanism. A strong pairwise comparator does not rescue optional stopping.

---

## 4. Challenge Generators Are Scientific Instruments

The generator is not a convenience. It is the scientific instrument that defines the world Agintor is learning from.

Let $Q_\phi$ be a generated challenge distribution parameterized by generator state $\phi$. The danger is:

$$
G_c \succ_{Q_\phi} G_p
\quad\text{but}\quad
G_c \not\succ_{R_D} G_p,
$$

where $R_D$ is the empirical distribution of real domain tasks.

This is generator overfitting: Agintor improves in the fake world but not the real one.

### 4.1 The generator health functional

Each generated task $x$ receives a task-health score:

$$
h_D(x)
=
\min
\Big(
 h_{\mathrm{valid}}(x),
 h_{\mathrm{answerable}}(x),
 h_{\mathrm{solvable}}(x),
 h_{\mathrm{realistic}}(x),
 h_{\mathrm{discriminative}}(x),
 h_{\mathrm{nontrivial}}(x),
 h_{\mathrm{nonleaking}}(x),
 h_{\mathrm{metamorphic}}(x)
\Big).
$$

A generator version $Q_\phi$ receives aggregate health:

$$
H(Q_\phi)
=
\min
\Big(
H_{\mathrm{syntax}},
H_{\mathrm{answer}},
H_{\mathrm{solvability}},
H_{\mathrm{realism}},
H_{\mathrm{discrimination}},
H_{\mathrm{mutation}},
H_{\mathrm{leakage}},
H_{\mathrm{template}},
H_{\mathrm{calibration}}
\Big).
$$

The minimum is essential. A generator that is realistic but answer-unreliable is bad. A generator that is answer-reliable but unrealistic is also bad.

### 4.2 Generator health dimensions

| Health dimension | Theoretical question |
|---|---|
| syntactic validity | Does the generated task belong to the domain language? |
| answer reliability | Is the private answer or acceptance predicate stable under independent derivation? |
| solvability | Does at least one known-good solver or witness satisfy the task? |
| realism | Does the task resemble real tasks along domain-relevant features? |
| discriminativity | Does the task separate known stronger systems from weaker systems? |
| nontriviality | Is the task neither always solved nor impossible? |
| mutation sensitivity | Does the task catch injected domain-specific faults? |
| template diversity | Is the task not merely a disguised clone of a small template family? |
| anti-leakage | Does it avoid exposing private answers or recognizably reused hidden structures? |
| metamorphic coherence | Do known transformations preserve required answer relations? |
| calibration stability | Does task difficulty remain stable across evaluator versions? |

### 4.3 Discriminativity is not difficulty

A hard task that everyone fails is not useful. An easy task that everyone passes is not useful. The most useful task lies near a capability frontier and produces high information.

Let $S\in\{0,1\}$ be success and let $\theta_G$ be a latent capability parameter. A generated task $x$ has information:

$$
I_x(\theta)
=
\mathbb{E}
\left[
\left(
\frac{\partial}{\partial\theta}
\log P(S\mid \theta,x)
\right)^2
\right].
$$

Good challenge generators maximize information subject to realism and answer reliability:

$$
\max_{Q_\phi}
\mathbb{E}_{x\sim Q_\phi}[I_x(\theta)]
\quad\text{subject to}\quad
H(Q_\phi)\ge \tau_H.
$$

A generator must not optimize only $I_x$; otherwise it will invent adversarial puzzles unrelated to the domain.

### 4.4 Baseline ladder monotonicity

A healthy generator should rank a known ladder of systems correctly. Let:

$$
B_0 \prec B_1 \prec \cdots \prec B_m
$$

be baseline agents or reference solvers with known increasing capability in domain $D$. A generator is healthy only if:

$$
\mathbb{E}_{x\sim Q_\phi}
\left[q_D(x,B_{j+1})-q_D(x,B_j)\right] > 0
\quad\forall j.
$$

A generator that cannot distinguish known weak and strong baselines has no right to decide whether Agintor improved.

---

## 5. Private Answers Are Not Always “Expected Outputs”

The phrase “private answer” is too narrow. Different domains require different answer structures.

### 5.1 Exact answer domains

For deterministic generated tool tasks, the private answer may be a denotation:

$$
A_D(x) = \llbracket e_x \rrbracket_{\rho_x},
$$

where $e_x$ is a typed expression and $\rho_x$ is the private environment.

This supports exact scoring:

$$
q(x,y)=\mathbb{1}[y=A_D(x)].
$$

Exact answer domains are valuable because they produce strong signal, but they saturate quickly unless expression depth, dependency structure, distractors, and type edge cases continue to expand.

### 5.2 Acceptance-predicate domains

For repository patches, the answer is usually not a unique patch. The answer is an acceptance predicate:

$$
A_D(x,y)=1
\iff
\Big(
\mathrm{FAIL\_TO\_PASS}(x,y)
\land
\mathrm{PASS\_TO\_PASS}(x,y)
\land
\mathrm{NoForbiddenDiff}(x,y)
\land
\mathrm{NoEnvTamper}(x,y)
\Big).
$$

Quality is not merely acceptance. A better patch may:

$$
\begin{aligned}
&\text{pass more hidden behavioral tests},\\
&\text{kill more seeded mutants},\\
&\text{preserve more invariants},\\
&\text{reduce defect search findings},\\
&\text{minimize unnecessary diff surface},\\
&\text{avoid performance regressions}.
\end{aligned}
$$

Thus repository quality is a vector:

$$
q_{\mathrm{repo}}(x,y)
=
\big(
q_{\mathrm{behavior}},
q_{\mathrm{regression}},
q_{\mathrm{mutation}},
q_{\mathrm{minimality}},
q_{\mathrm{performance}},
q_{\mathrm{maintainability}}
\big).
$$

Only the grounded axes may support capability promotion. Maintainability may require human or expert-review authority unless mechanically tied to defects.

### 5.3 Transition-system domains

For service-state tasks, the answer is a set of valid traces, not a single final string. Let:

$$
s_{i+1}=T(s_i,a_i),
\quad
I(s_i)=1,
\quad
\Phi(s_n)=1.
$$

An artifact $y=(a_1,\ldots,a_n)$ is valid if:

$$
A_D(x,y)=1
\iff
\left(
\forall i,\; a_i\in\mathcal{A}_{\mathrm{allowed}}(s_i)
\right)
\land
\left(
\forall i,\; I(s_i)=1\right)
\land
\Phi(s_n).
$$

A better result may use fewer irreversible side effects, achieve stronger idempotence, recover from intermediate failures, or maintain invariants under replay.

### 5.4 Metamorphic domains

Sometimes no exact answer is available, but relations between answers are known. For a transformation $\tau$ and relation $R_m$:

$$
R_m\big(y(x),y(\tau(x))\big)=1.
$$

Metamorphic testing does not solve every oracle problem, but it converts some unknown absolute answers into known relational checks.

A metamorphic quality axis is:

$$
q_{m}(G)
=
\mathbb{E}_{x\sim P_D,\tau\sim M_D}
\left[
\mathbb{1}\left(R_m(Y_G(x),Y_G(\tau(x)))\right)
\right].
$$

A child is better when it preserves more required relations under realistic transformations.

### 5.5 Preference domains

For open-ended answers, the “answer” may be a user preference utility:

$$
u_u(x,y)\in\mathbb{R}.
$$

Without human/user feedback or observable downstream outcomes, $\nu_u$ is not grounded. An LLM judge can estimate a proxy:

$$
\hat{\nu}_{\mathrm{LLM}}(x,y),
$$

but this proxy has bounded authority and cannot certify core capability improvement. It may support preference-model training, triage, and task selection. It may not replace grounded outcomes.

---

## 6. Realism: Generated Worlds Must Be Tethered to Real Worlds

The central transfer problem is:

$$
\Delta_{R_D}(G_c,G_p)
\quad\text{versus}\quad
\Delta_{Q_\phi}(G_c,G_p).
$$

A generator can only justify real-domain promotion if improvement under $Q_\phi$ transfers to $R_D$.

### 6.1 Distribution discrepancy bound

Let $\mathcal{F}$ be a class of domain-relevant quality-difference functions:

$$
f_{G_c,G_p}(x)
=
q_D(x,Y_{G_c}(x))-q_D(x,Y_{G_p}(x)).
$$

Define an integral probability metric:

$$
\mathrm{IPM}_{\mathcal{F}}(R_D,Q_\phi)
=
\sup_{f\in\mathcal{F}}
\left|
\mathbb{E}_{x\sim R_D}[f(x)]
-
\mathbb{E}_{x\sim Q_\phi}[f(x)]
\right|.
$$

Then:

$$
\Delta_{R_D}
\ge
\Delta_{Q_\phi}
-
\mathrm{IPM}_{\mathcal{F}}(R_D,Q_\phi)
-
\eta_{\mathrm{answer}}
-
\eta_{\mathrm{measurement}}.
$$

This inequality is the formal antidote to fake-world optimization. If the realism gap is large, Agintor may still learn, but the promotion scope must say:

$$
\text{generated-domain improvement only.}
$$

### 6.2 Slice-aware realism

Real domains are mixtures of slices:

$$
R_D = \sum_{s\in\mathcal{S}} \pi_R(s) R_{D,s}.
$$

Generated domains are also mixtures:

$$
Q_\phi = \sum_{s\in\mathcal{S}} \pi_Q(s) Q_{\phi,s}.
$$

The real-domain effect estimate should be slice-reweighted:

$$
\widehat{\Delta}_{R_D}
=
\sum_{s\in\mathcal{S}}
\pi_R(s)\widehat{\Delta}_{Q_{\phi,s}}.
$$

If a slice has insufficient evidence, the promotion must exclude that slice. A generated task system that over-samples artificial edge cases may still be useful, but it must not claim broad real-domain progress.

### 6.3 Realism is an audit, not a vibe

Realism requires at least four audits:

| Audit | Meaning |
|---|---|
| feature-distribution audit | generated tasks match real tasks on domain features |
| slice-coverage audit | important real slices have generated counterparts |
| human/repo audit | experts judge whether tasks reflect realistic failure modes |
| transfer audit | improvements on generated tasks predict improvements on held-out real tasks |

The final audit is most important:

$$
\mathrm{Transfer}(Q_\phi\to R_D)
=
\mathrm{Corr}\left(
\widehat{\Delta}_{Q_\phi},
\widehat{\Delta}_{R_D^{\mathrm{heldout}}}
\right).
$$

If transfer is weak, the generator may remain useful for exploration but not for promotion.

---

## 7. The Anti-Junk Generator Program

Generated tasks become junk when a generator optimizes surface validity while losing semantic contact with the domain. Junk can be easy, impossible, unrealistic, answer-ambiguous, leak-prone, or overly patterned.

### 7.1 Junk taxonomy

| Junk type | Symptom | Consequence |
|---|---|---|
| tautological | every artifact passes | no learning signal |
| impossible | no artifact passes | false regressions or noise |
| unrealistic | tasks do not resemble real work | fake-world optimization |
| answer-unstable | reference solvers disagree | false labels |
| template-cloned | repeated hidden structure | memorization |
| adversarial-puzzle | high difficulty but irrelevant | wrong capability pressure |
| judge-bait | rewards verbosity or style | trace/style Goodharting |
| leakage-shaped | contains answer-revealing artifacts | invalid private signal |
| degenerate-frontier | tailored to one parent weakness only | poor generalization |

### 7.2 Generator mutation testing

A generator should be tested against known bad solvers and injected defects. Let $\mathcal{B}^{-}$ be a corpus of bad systems, and let $\mathcal{B}^{+}$ be known-good systems. A generator has mutation-kill health:

$$
H_{\mathrm{mutation}}(Q_\phi)
=
\Pr_{x\sim Q_\phi,\,B^-\sim\mathcal{B}^{-}}
\left[A_D(x,Y_{B^-}(x))=0\right].
$$

It has positive-control health:

$$
H_{\mathrm{positive}}(Q_\phi)
=
\Pr_{x\sim Q_\phi,\,B^+\sim\mathcal{B}^{+}}
\left[A_D(x,Y_{B^+}(x))=1\right].
$$

A generator that catches bad systems but also rejects good systems is not healthy. The generator must satisfy both:

$$
H_{\mathrm{mutation}}\ge \tau_-
\quad\text{and}\quad
H_{\mathrm{positive}}\ge \tau_+.
$$

### 7.3 Held-out generator templates

If Agintor sees the same generator templates during mutation and evaluation, it can learn template fingerprints. Therefore generator templates must have lifecycle states:

$$
\mathrm{design}\to\mathrm{train}\to\mathrm{validation}\to\mathrm{confirmatory}\to\mathrm{retired}.
$$

The child candidate may receive diagnostic feedback from train templates, but capability promotion must depend on validation or confirmatory templates that were not exposed through mutation feedback.

### 7.4 Generator adversarial review

Generators should face red-team pressure:

$$
\max_{G_{\mathrm{hack}}}
\mathbb{E}_{x\sim Q_\phi}
\left[A_D(x,Y_{G_{\mathrm{hack}}}(x))\right]
\quad\text{subject to}\quad
G_{\mathrm{hack}}\text{ violates intended semantics.}
$$

If such a hack system scores well, the generator or acceptance predicate is unhealthy.

---

## 8. Statistical Promotion Without Adaptive Self-Deception

Adaptive probing is useful for diagnosis and dangerous for promotion. If Agintor samples until a child looks better, it will eventually false-promote.

The remedy is not to ban adaptivity. The remedy is to separate exploratory probing from confirmatory testing, or to use anytime-valid inference that remains valid under optional stopping.

### 8.1 Two-phase promotion

The cleanest design is:

$$
\text{explore} \quad \longrightarrow \quad \text{lock} \quad \longrightarrow \quad \text{confirm}.
$$

#### Explore

Adaptive frontier probes identify weaknesses, estimate difficulty, and select slices. Explore-stage data may inform debugging and task selection. It may not promote.

#### Lock

Before confirmatory evaluation, the system fixes:

$$
\left(N, P_D^{\mathrm{confirm}}, \mathcal{A}_{\mathrm{axes}}, \epsilon, \rho, \alpha\right).
$$

#### Confirm

Promotion uses only confirmatory data. The decision is:

$$
\mathrm{Promote}
\iff
L_{\mathrm{confirm}} > \epsilon
\land
\mathrm{NoProtectedRegression}
\land
\mathrm{HealthFloorPasses}.
$$

This is brutally simple and very hard to fool.

### 8.2 Anytime-valid alternative

When fixed budgets are too rigid, use a confidence sequence. Let $d_i\in[-1,1]$ be paired quality deltas and $\mathcal{F}_{i-1}$ be the history before observing $d_i$. The task-selection policy may choose $x_i$ using $\mathcal{F}_{i-1}$, but not using $d_i$.

An anytime-valid confidence sequence satisfies:

$$
\Pr\left(\exists n\ge 1:\; \mu\notin \mathrm{CS}_n\right)\le \alpha.
$$

Promotion is legal at a stopping time $\tau$ only if:

$$
\inf \mathrm{CS}_{\tau} > \epsilon.
$$

This supports adaptive frontier probing without allowing “sample until lucky” false discoveries.

### 8.3 Alpha is a global resource

Agintor evaluates many children. Therefore each candidate cannot spend a fresh $\alpha=0.05$ as though it were the only test ever run.

Let $\alpha_i$ be the risk spent on candidate $i$. The system must satisfy:

$$
\sum_{i=1}^{\infty} \alpha_i \le \alpha_{\mathrm{global}}.
$$

The promotion ledger must record:

$$
\alpha_{\mathrm{spent}}(G_c,G_p,D,a).
$$

A progress claim without an alpha receipt is not a scientific claim; it is an observation.

### 8.4 Frontier adaptivity must be parent-symmetric

If the generator adapts to weaknesses in the parent but not the child, it may create a biased test. If it adapts to both observed outcomes, it can overfit to noise. A safe policy is:

$$
P_i(x) = P_i(x\mid \mathcal{F}_{i-1}^{\mathrm{public}}, G_p, G_c, \text{predeclared rule}),
$$

where $P_i$ is fixed before observing the paired result on $x_i$. The rule may use public metadata and historical frontier estimates, but must not choose the next task because “the child is currently close to winning.”

---

## 9. LLM Judges: Useful, Weak, and Non-Core

LLM judges are useful instruments. They are not core capability oracles.

Their known weaknesses include position bias, verbosity bias, self-preference bias, and limited reasoning reliability in open-ended comparisons. Blind artifact duels, position swaps, length normalization, and calibration help, but they do not turn subjective preference proxies into high-authority capability evidence.

### 9.1 Proper role of LLM judges

LLM judges may support:

$$
\begin{aligned}
&\text{triage},\\
&\text{defect hypothesis generation},\\
&\text{rubric decomposition},\\
&\text{preference-model bootstrapping},\\
&\text{candidate task filtering},\\
&\text{human-review prioritization}.
\end{aligned}
$$

They may not independently certify:

$$
\begin{aligned}
&\text{repository patch correctness},\\
&\text{service-state correctness},\\
&\text{private answer equality},\\
&\text{strategic superiority},\\
&\text{architecture superiority},\\
&\text{broad capability improvement}.
\end{aligned}
$$

### 9.2 Judge evidence cap

Let $J$ be a judge-derived preference signal. Its likelihood contribution must be capped:

$$
\left|\log \mathrm{LR}(J)\right| \le \kappa_{\mathrm{judge}},
\quad
\kappa_{\mathrm{judge}} \ll \kappa_{\mathrm{grounded}}.
$$

Even many judge votes cannot become high-authority evidence unless grounded subclaims are independently checked:

$$
\sum_{j=1}^{m}\log \mathrm{LR}(J_j)
\le
K_{\mathrm{judge\_group}}.
$$

### 9.3 Pairwise judges as preference learners

For user-facing open-ended answers, a judge can help estimate a preference model:

$$
P(y_1\succ_u y_0\mid x)
=\sigma\left(r_u(x,y_1)-r_u(x,y_0)\right).
$$

But the promotion type must be:

$$
\mathrm{preference\_hypothesis}
\quad\text{or}\quad
\mathrm{preference\_promotion},
$$

not core capability promotion, unless human/user outcomes ground $r_u$.

---

## 10. Lessons from AlphaEvolve, FunSearch, and OpenSage

### 10.1 AlphaEvolve’s lesson

AlphaEvolve is powerful in domains where proposed programs can be run, verified, and scored with objective automated evaluators. Its success does not remove the oracle problem; it highlights the importance of choosing domains where the evaluator is executable and quantitatively meaningful.

For Agintor, the lesson is:

$$
\text{evolution works when the evaluator is real.}
$$

AlphaEvolve-style search is appropriate for Agintor’s bounded domains only after the domain evidence contract exists. Without the contract, evolutionary pressure optimizes whatever proxy is easiest to exploit.

### 10.2 FunSearch’s lesson

FunSearch shows the power of evolving programs against systematic evaluators. Its deeper lesson is that the output being evolved should be interpretable and executable enough for the evaluator to reject hallucinated ideas.

For Agintor, this argues for representing many challenges and policies as executable or relational objects:

$$
\text{artifact} \longrightarrow \text{run} \longrightarrow \text{measure} \longrightarrow \text{select}.
$$

This is strongest in tool workflows, generated DSL tasks, repository tests, and state machines.

### 10.3 OpenSage’s lesson

OpenSage-style self-programming agent construction expands the search space: agents can generate sub-agents, tools, and memory structures. That is valuable, but it also expands the evaluation problem. A self-generated topology or toolset is not progress unless a domain evidence institution can measure its effect.

OpenSage-like mechanisms should be used in two carefully separated ways:

$$
\begin{aligned}
&\text{Candidate generation: produce new MAS topologies, tools, and memory policies.}\\
&\text{Instrument generation: propose challenge generators, fuzzers, metamorphic relations, and defect searchers.}
\end{aligned}
$$

The second category must not gain authority by being generated. It starts as a hypothesis and must pass generator-health and validator-health audits before contributing to promotion.

The synthesis is:

$$
\text{OpenSage expands what can be proposed;}\quad
\text{AlphaEvolve explains how to evolve against objective evaluators;}\quad
\text{Agintor must build the evaluators first.}
$$

---

## 11. Domain Blueprints

### 11.1 Repository patch domain

#### Task source

Repository tasks should be a mixture:

$$
P_{\mathrm{repo}}
=\lambda_1 P_{\mathrm{real\_issues}}
+\lambda_2 P_{\mathrm{synthetic\_bugs}}
+\lambda_3 P_{\mathrm{mutation\_faults}}
+\lambda_4 P_{\mathrm{metamorphic\_repo}}.
$$

Real issues provide realism. Synthetic and mutation-generated tasks provide frontier density and controlled labels. Metamorphic tasks probe invariants.

#### Answer mechanism

A repository patch has an acceptance predicate:

$$
A_{\mathrm{repo}}(x,y)=
T_{\mathrm{fail\to pass}}(x,y)
\land
T_{\mathrm{pass\to pass}}(x,y)
\land
D_{\mathrm{allowed}}(x,y)
\land
E_{\mathrm{stable}}(x,y).
$$

Better-than quality is vector-valued:

$$
q_{\mathrm{repo}}
=
\big(
q_{\mathrm{hidden}},
q_{\mathrm{regression}},
q_{\mathrm{mutant}},
q_{\mathrm{fuzz}},
q_{\mathrm{minimal}},
q_{\mathrm{perf}}
\big).
$$

A child that passes the same visible tests as the parent but kills more mutants or survives more fuzz cases is genuinely better. A child that passes the same tests with fewer tokens is only more efficient.

#### Generator health

Repository generator health requires:

$$
H_{\mathrm{repo\_gen}}
=\min
\left(
H_{\mathrm{issue\_realism}},
H_{\mathrm{test\_validity}},
H_{\mathrm{env\_stability}},
H_{\mathrm{bug\_solvability}},
H_{\mathrm{mutant\_kill}},
H_{\mathrm{slice\_coverage}}
\right).
$$

The most important realism audit is whether generated bugs predict performance on held-out human-validated repository issues.

### 11.2 Stateful service domain

A service task is generated from a state machine:

$$
\mathcal{S}=(\mathcal{Z},\mathcal{A},T,I,\Phi,C),
$$

where $\mathcal{Z}$ is state space, $\mathcal{A}$ action space, $T$ transition function, $I$ invariants, $\Phi$ goal condition, and $C$ side-effect cost.

An artifact is an action trace:

$$
y=(a_1,\ldots,a_n).
$$

Quality is:

$$
q_{\mathrm{service}}(x,y)
=\big(
q_{\mathrm{final}},
q_{\mathrm{invariant}},
q_{\mathrm{idempotence}},
q_{\mathrm{replay}},
q_{\mathrm{minimal\_sideeffect}},
q_{\mathrm{recovery}}
\big).
$$

A better child reaches the desired final state while preserving more invariants, performing fewer irreversible actions, and remaining correct under replay or partial failure.

The reference answer is not one path; it is the set:

$$
\mathcal{Y}^{\star}(x)
=
\left\{y:\Phi(s_n)=1\land\forall i, I(s_i)=1\right\}.
$$

### 11.3 Generated tool-workflow domain

Generated tool tasks should be built from a typed DSL with denotational semantics:

$$
\llbracket e \rrbracket_{\rho}:\mathcal{E}\times\mathcal{R}\to\mathcal{V}.
$$

Task difficulty is controlled by:

$$
d(x)=
\big(
\mathrm{depth},
\mathrm{arity},
\mathrm{type\_mix},
\mathrm{dependency\_width},
\mathrm{distractor\_count},
\mathrm{numeric\_edgecases}
\big).
$$

Quality axes:

$$
q_{\mathrm{tool}}
=\big(
q_{\mathrm{answer}},
q_{\mathrm{tool\_grounding}},
q_{\mathrm{dependency}},
q_{\mathrm{type\_robust}},
q_{\mathrm{distractor\_resistance}}
\big).
$$

This is one of Agintor’s strongest early domains because private answers are cheap and reliable if the DSL interpreter is independently audited.

### 11.4 Structured memory retrieval domain

Memory tasks should be generated from a private knowledge graph:

$$
K=(V,E,\ell,\tau),
$$

with typed nodes, typed edges, labels, and temporal metadata. A query $q$ denotes an answer set:

$$
A_{\mathrm{mem}}(K,q)=\{v\in V:\;K\models q(v)\}.
$$

Metamorphic relations are especially powerful:

$$
\begin{aligned}
&\text{adding unrelated nodes should not change the answer},\\
&\text{isomorphic renaming should preserve the answer},\\
&\text{stale memory should lose to newer contradictory memory when policy says so},\\
&\text{removing a distractor should not change a correct answer}.
\end{aligned}
$$

Quality axes:

$$
q_{\mathrm{mem}}
=\big(
q_{\mathrm{exact}},
q_{\mathrm{recency}},
q_{\mathrm{multi\_hop}},
q_{\mathrm{conflict}},
q_{\mathrm{distractor}},
q_{\mathrm{provenance}}
\big).
$$

This domain can distinguish good from better by expanding graph topology, conflict structure, and query compositionality.

### 11.5 End-to-end structured tasks

End-to-end tasks combine memory, tools, and multi-step artifact production. They should be generated compositionally:

$$
x = x_{\mathrm{mem}} \oplus x_{\mathrm{tool}} \oplus x_{\mathrm{format}} \oplus x_{\mathrm{constraint}}.
$$

The answer mechanism decomposes into subclaims:

$$
A_{\mathrm{e2e}}(x,y)=
\bigwedge_{j=1}^{m} A_j(x,y).
$$

Quality is not a single exact score but a weighted vector of subclaim success:

$$
q_{\mathrm{e2e}}(x,y)=
\sum_{j=1}^{m} w_j\,\mathbb{1}[A_j(x,y)].
$$

A child can be better by fixing a subskill even if it still fails the entire end-to-end task. This matters for evolutionary learning: intermediate progress must be visible without being confused with deployment readiness.

---

## 12. Human and Real-World Feedback

Some qualities cannot be generated honestly. Taste, usefulness, strategic judgment, and architectural elegance require feedback from the world or from people with domain preferences.

### 12.1 Preference is a different authority type

Let user utility be:

$$
u_u(x,y).
$$

A pairwise human preference label is:

$$
\ell_u(x,y_0,y_1)\in\{y_0\succ y_1, y_1\succ y_0, \mathrm{tie}, \mathrm{incomparable}\}.
$$

This supports a preference model:

$$
P(y_1\succ_u y_0\mid x)=
\sigma\left(r_u(x,y_1)-r_u(x,y_0)\right).
$$

But this does not imply the child is more capable in a grounded domain. It implies the child better satisfies observed preferences.

### 12.2 Human audits for generator realism

Humans are especially valuable not as judges of every artifact, but as auditors of the evaluation world:

$$
\text{human effort} \gg \text{when spent on generator realism rather than artifact-by-artifact scoring.}
$$

The highest-leverage human questions are:

| Audit question | Why it matters |
|---|---|
| Does this generated repo issue resemble a real bug? | prevents fake-world optimization |
| Is the task underspecified? | prevents ambiguous answer labels |
| Would a competent engineer accept this test as meaningful? | prevents brittle evaluator artifacts |
| Does the generator cover real issue slices? | prevents narrow improvement claims |
| Are the private tests too tied to one implementation? | prevents overfitting to reference patches |

Human labels should primarily calibrate $H_{\mathrm{realism}}$, not act as endless expensive artifact scores.

---

## 13. Co-Evolving Generators Without Self-Confirmation

Agintor may eventually evolve its own challenge generators, fuzzers, tool worlds, and metamorphic relation finders. This is powerful and dangerous.

The rule is:

$$
\text{Generated instruments may propose evidence. They do not grant themselves authority.}
$$

### 13.1 Two-timescale evolution

Candidate agents and evaluation instruments must evolve on different timescales:

$$
\begin{aligned}
&G\text{-loop: evolves MAS genomes against frozen evaluation contracts},\\
&E\text{-loop: evolves evaluation instruments against retired corpora and human/realism audits}.
\end{aligned}
$$

The $G$-loop must not be evaluated by instruments that changed in response to the current child. The $E$-loop must be audited on data not produced by the current $G$-loop lineage.

### 13.2 Instrument promotion

A generated challenge generator $Q_{\phi'}$ is promoted only if it improves instrument quality:

$$
H(Q_{\phi'}) - H(Q_{\phi}) > \epsilon_H
$$

under held-out generator audits. It is not promoted because it makes the current Agintor child look better.

### 13.3 Cross-instrument agreement

For high-authority use, independent generator families should agree directionally:

$$
\mathrm{sign}\left(\widehat{\Delta}_{Q^{(1)}}\right)
=
\mathrm{sign}\left(\widehat{\Delta}_{Q^{(2)}}\right)
=\cdots
$$

when they claim the same real-domain slice. Disagreement does not imply failure; it implies narrower scope or abstention.

---

## 14. Promotion Semantics

A promotion signal must classify the kind of improvement.

### 14.1 Capability promotion

Capability promotion requires:

$$
\exists a\in\mathcal{A}_{\mathrm{cap}}:
L_a > \epsilon_a
$$

and:

$$
\forall b\in\mathcal{A}_{\mathrm{protected}}:
U_b \ge -\rho_b.
$$

It also requires:

$$
H(Q_\phi)\ge\tau_Q,
\quad
H(A_D)\ge\tau_A,
\quad
H(V_D)\ge\tau_V,
\quad
\alpha_{\mathrm{spent}}\le\alpha_{\mathrm{available}}.
$$

### 14.2 Efficiency promotion

Efficiency promotion requires quality equivalence:

$$
\forall a\in\mathcal{A}_{\mathrm{cap}}:
L_a\ge -\rho_a
\quad\text{and}\quad
U_a\le \rho_a,
$$

plus cost improvement:

$$
L_{\mathrm{cost}} > \epsilon_{\mathrm{cost}}.
$$

This is useful but does not update capability priors.

### 14.3 Preference promotion

Preference promotion requires human/user-grounded preference evidence:

$$
L_{\mathrm{pref}} > \epsilon_{\mathrm{pref}}.
$$

LLM-judge evidence may reduce human labeling cost or prioritize candidates, but cannot alone create high-authority preference promotion.

### 14.4 Subskill promotion

Subskill promotion is allowed when a child improves a grounded subclaim while still failing a full release gate:

$$
\exists a\in\mathcal{A}_{\mathrm{subskill}}:
L_a>\epsilon_a
\quad\land\quad
A_{\mathrm{release}}(G_c)=0.
$$

This is important for evolution. A candidate can be evolutionarily valuable before it is deployable.

### 14.5 No-progress result

If exact tasks saturate and no frontier, metamorphic, defect, or hidden-slice evidence exists, the correct output is:

$$
\mathrm{NoCapabilitySignal}.
$$

This is not a failure of the agent. It is a failure of the evaluation world to support capability measurement.

---

## 15. Defect Search as a Better-Than Engine

A powerful way to distinguish good from better is to attack both artifacts.

Let $D(x,y)$ be a set of verified defects found in artifact $y$ for task $x$. Define severity-weighted defect loss:

$$
L_{\mathrm{defect}}(x,y)=
\sum_{d\in D(x,y)} s(d).
$$

Then:

$$
q_{\mathrm{defect}}(x,y)=1-\mathrm{clip}\big(L_{\mathrm{defect}}(x,y),0,1\big).
$$

A child is better if:

$$
\mathbb{E}[L_{\mathrm{defect}}(x,Y_{G_c}(x))]
<
\mathbb{E}[L_{\mathrm{defect}}(x,Y_{G_p}(x))].
$$

Defect search should be adversarial but independently verified. An LLM critic may propose possible defects, but only verified defects count:

$$
D(x,y)=\{d:\mathrm{CriticProposes}(d)\land\mathrm{VerifierConfirms}(d)\}.
$$

This preserves the useful creativity of model critics without letting judge speculation become reward.

---

## 16. Metamorphic Better-Than Testing

Metamorphic tests are not merely pass/fail; they produce robustness gradients.

Let $M_D$ be a distribution over transformations $\tau$ and relations $R_\tau$. A system’s metamorphic robustness is:

$$
\mu_M(G)
=
\mathbb{E}_{x\sim P_D,\tau\sim M_D}
\left[
\mathbb{1}\left(R_\tau(Y_G(x),Y_G(\tau(x)))\right)
\right].
$$

The child is better if:

$$
\mu_M(G_c)-\mu_M(G_p)>\epsilon_M.
$$

This is especially valuable when exact answers are expensive but invariants are cheap.

Examples:

| Domain | Metamorphic relation |
|---|---|
| tool expression | algebraically equivalent forms yield same value |
| memory retrieval | adding irrelevant facts does not change answer |
| repository patch | unrelated tests remain passing after patch |
| service tasks | replaying idempotent request does not duplicate side effects |
| data transformation | row order permutation preserves aggregate |

---

## 17. Capability, Strategy, Taste, and Architecture

The user feedback is correct: this design is not general magic. It should explicitly say what it cannot validate.

### 17.1 Strategy

A better strategy can be promoted only if strategy is measured through an environment:

$$
\Delta_{\mathrm{strategy}}
=
\mathbb{E}_{e\sim E}\left[R(e,G_c)-R(e,G_p)\right].
$$

Without an environment $E$ and reward $R$ grounded in outcomes, “better strategy” is merely an opinion.

### 17.2 Taste

Taste requires a preference population:

$$
U_{\mathrm{taste}}(G)=\mathbb{E}_{u\sim\mathcal{U},x\sim P}\left[\nu_u(x,Y_G(x))\right].
$$

Without humans, user telemetry, or explicit style contracts, taste improvement cannot be core capability improvement.

### 17.3 Architecture

A better architecture is not one that looks more sophisticated. It is one whose causal contribution survives ablation:

$$
\Delta_{\mathrm{arch}}(c)
=
\mathbb{E}_{x\sim P_D}
\left[q_D(x,Y_{G}(x))-q_D(x,Y_{G\setminus c}(x))\right].
$$

Architecture claims require counterfactual evidence. Otherwise Agintor will reward reviewer theater, tool bloat, and topology ornamentation.

---

## 18. The Core Theorems Agintor Should Behave As If They Are True

These are not formal theorems of mathematics without additional assumptions. They are design theorems: the system should be built so that violating them is impossible.

### 18.1 No grounded domain, no capability promotion

If a domain has no reliable answer mechanism, no realism audit, and no grounded outcome function, then:

$$
\nexists\; \mathrm{CapabilityPromotion}(D).
$$

The system may collect preferences, diagnostics, and human review queues. It may not claim self-improvement.

### 18.2 Generated-world promotion requires transfer evidence

If:

$$
\mathrm{IPM}_{\mathcal{F}}(R_D,Q_\phi)>\Gamma,
$$

then:

$$
\mathrm{Promote}_{R_D}(G_c,G_p)=0,
$$

even if:

$$
\mathrm{Promote}_{Q_\phi}(G_c,G_p)=1.
$$

The correct scope is generated-domain improvement.

### 18.3 Judge-only wins are not capability wins

If all positive evidence comes from weak pairwise judges:

$$
A_{\mathrm{eff}}\le A_{\mathrm{judge}},
$$

then capability promotion is illegal unless judge claims are converted into checkable subclaims and independently verified.

### 18.4 Optional stopping invalidates naive promotion

If task sampling continues until:

$$
\widehat{\Delta}_n > \epsilon,
$$

without fixed budgets or anytime-valid correction, then the false promotion probability is uncontrolled:

$$
\Pr\left(\exists n:\widehat{\Delta}_n>\epsilon\mid \Delta=0\right)
\not\le \alpha.
$$

### 18.5 Capability and efficiency are orthogonal updates

If:

$$
\Delta_Q \approx 0
\quad\text{and}\quad
\Delta_C>0,
$$

then:

$$
\mathrm{Update}=\mathrm{EfficiencyOnly}.
$$

Capability priors must not move.

---

## 19. The Practical Bounded-Domain Roadmap

Agintor should not begin with open-ended evaluation. It should mature domains in this order:

### Stage 1: Deterministic generated tool tasks

Highest answer reliability:

$$
A_D(x)=\llbracket e_x\rrbracket.
$$

Best for early capability gradients.

### Stage 2: Structured memory retrieval

High answer reliability if generated from private knowledge graphs:

$$
A_D(K,q)=\{v:K\models q(v)\}.
$$

Best for memory/tool/e2e subskills.

### Stage 3: Stateful service tasks

High authority if state transition model is formal:

$$
s_{i+1}=T(s_i,a_i).
$$

Best for side effects, retries, idempotence, and real workflow discipline.

### Stage 4: Repository patch tasks

High value, harder realism. Requires real issue corpora, hidden tests, mutation tests, fuzzing, and human/repo realism audits.

### Stage 5: Open-ended preference tasks

Useful but not core capability unless grounded by humans, users, or downstream outcomes.

---

## 20. What the Implementor Should Preserve

This document avoids code, but the implementor must preserve the following semantic boundaries.

### 20.1 Frozen contracts

A candidate must be evaluated against an evaluation contract that was not adapted after seeing its outputs:

$$
\mathcal{C}_D \perp Y_{G_c}\mid \mathrm{precommit}.
$$

### 20.2 Separate archives

The archive of capability improvements and the archive of efficiency improvements must be different mathematical objects:

$$
\mathcal{A}_{\mathrm{capability}}\cap\mathcal{A}_{\mathrm{efficiency}}\ne \text{same update semantics}.
$$

### 20.3 Generator versioning

Every promotion is relative to a generator version:

$$
\sigma = \sigma(G_p,G_c,D,Q_\phi,A_D,V_D,S_D).
$$

Changing $Q_\phi$ changes the claim.

### 20.4 Scope-preserving feedback

Feedback sent to the mutator may identify weak axes and slices, but not private answers, hidden templates, or confirmatory examples.

### 20.5 Explicit abstention

When the evaluation world cannot distinguish good from better, it must say so:

$$
\mathrm{Abstain}\neq\mathrm{Reject}\neq\mathrm{Promote}.
$$

Abstention is an honest scientific result.

---

## 21. Final Design Thesis

The system Agintor needs is not:

$$
\text{LLM mutator} + \text{oracle comparator} + \text{archive}.
$$

It is:

$$
\text{LLM/agentic proposal engine}
+
\text{bounded domain evidence institutions}
+
\text{generator health science}
+
\text{private answer mechanisms}
+
\text{realism transfer audits}
+
\text{valid sequential statistics}
+
\text{scope-limited promotion}.
$$

The hard thing to build is the middle:

$$
\boxed{
\text{Challenge generators with reliable private answers and audited realism.}
}
$$

That is where the engineering effort should go. Not into making a clever comparator. Not into trusting open-ended judges. Not into pretending generated tasks are real by default.

The final promotion rule is:

$$
\mathrm{CapabilityPromote}(G_c,G_p,D)=1
$$

only if:

$$
\begin{aligned}
&\exists a\in\mathcal{A}_{\mathrm{cap}}:\; L_a>\epsilon_a,\\
&\forall b\in\mathcal{A}_{\mathrm{protected}}:\; U_b\ge -\rho_b,\\
&H(Q_\phi),H(A_D),H(V_D),H(R_D)\ge \tau,\\
&\mathrm{StatisticalReceipt}(\alpha) = \mathrm{valid},\\
&\mathrm{Leakage}=0,\\
&\mathrm{Scope}=\text{explicit and bounded}.
\end{aligned}
$$

If these conditions fail, Agintor may still learn diagnostics, preferences, or efficiency. It may not claim capability progress.

---

## References

[VME] **Validated MAS Evolution — Synthesis for Agintor.** Uploaded internal design document.

[AlphaEvolve] Alexander Novikov et al. **AlphaEvolve: A coding agent for scientific and algorithmic discovery.** arXiv:2506.13131, 2025. <https://arxiv.org/abs/2506.13131>

[AlphaEvolve Blog] Google DeepMind. **AlphaEvolve: A Gemini-powered coding agent for designing advanced algorithms.** 2025. <https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/>

[AlphaEvolve Cloud] Google Cloud. **AlphaEvolve on Google Cloud: AI for agentic discovery and optimization.** 2025. <https://cloud.google.com/blog/products/ai-machine-learning/alphaevolve-on-google-cloud>

[FunSearch] Bernardino Romera-Paredes et al. **Mathematical discoveries from program search with large language models.** Nature, 2024. <https://www.nature.com/articles/s41586-023-06924-6>

[FunSearch Blog] Google DeepMind. **FunSearch: Making new discoveries in mathematical sciences using Large Language Models.** 2023. <https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/>

[OpenSage] Hongwei Li et al. **OpenSage: Self-programming Agent Generation Engine.** arXiv:2602.16891, 2026. <https://arxiv.org/abs/2602.16891>

[MT-Bench] Lianmin Zheng et al. **Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena.** NeurIPS Datasets and Benchmarks, 2023. <https://arxiv.org/abs/2306.05685>

[SWE-bench Verified] SWE-bench. **SWE-bench Verified.** <https://www.swebench.com/verified.html>

[OpenAI SWE-bench Verified] OpenAI. **Introducing SWE-bench Verified.** 2024, updated 2025. <https://openai.com/index/introducing-swe-bench-verified/>

[Metamorphic Survey] Sergio Segura et al. **A Survey on Metamorphic Testing.** IEEE Transactions on Software Engineering, 2016. <https://www.computer.org/csdl/journal/ts/2016/09/07422146/13rRUx0gewQ>

[Oracle Problem Survey] Earl T. Barr et al. **The Oracle Problem in Software Testing: A Survey.** IEEE Transactions on Software Engineering, 2015. <https://dl.acm.org/doi/10.1109/TSE.2014.2372785>

[Hypothesis] Hypothesis documentation. **Property-based testing for Python.** <https://hypothesis.readthedocs.io/>
