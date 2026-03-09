# Agintor: Evolutionary Discovery of Self-Programming Agent Topology, Memory, Tooling, and Control

A reconstruction-complete, performance-optimized system specification

---

## Abstract

Agintor treats agent-development-kit design as bounded evolutionary search over executable runtime code rather than as prompt tuning around a fixed agent. A candidate runtime mutates only four coupled symbolic decision surfaces inside an immutable self-programming shell: topology, hierarchical memory, dynamic tooling, and budget-verification control. The shell supplies clone-on-run sub-agents, graph-structured short-term and long-term memory, category-organized tools with isolated runtimes, exact or near-exact task verifiers, deterministic logging, and immutable safety and benchmark adapters. Candidate runtimes are proposed through exact SEARCH/REPLACE patches, screened by staged evaluation, and retained in an objective-conditioned quality-diversity archive.

This specification makes the method closed and reconstruction-ready. It defines the fixed shell, mutable genotype, mandatory schemas, runtime state machine, hard invalidation rules, primary and robustness objectives, predictor family, archive descriptors, controller credit assignment, topology search, memory policy, tool synthesis and reuse, verification control, mutation curriculum, compute gates, defaults, and failure modes. The decisive design choice is to co-evolve the interaction surfaces among topology, memory, tools, and control rather than treating them as independent modules. The performance-oriented refinements in this document favor stable search, stronger diversity preservation, lower wasted compute, and tighter runtime efficiency without relaxing determinism or safety.

## 1. Scope, Design Goals, and Core Notation

Agintor searches over runtime logic, not over a benchmark-specific prompt. The search target is the executable code that decides when to reuse or create sub-agents, what evidence to retrieve or summarize, which tools to reuse or synthesize, which model and verifier to spend budget on, and when the system should stop.

The method contains two nested loops. The outer loop performs bounded program search over selected source files. The inner loop is the task-time runtime that executes those files to solve a concrete task. Search therefore acts on the control surface that determines downstream agent behavior rather than on behavior directly.

The design is constrained by four goals:

1. **Bounded mutability.** Search may rewrite only designated methods inside an immutable shell.
2. **Deterministic replay.** Parent-child comparisons use common random numbers, frozen adapters, and content-addressed tool environments.
3. **Subsystem co-evolution.** Topology, memory, tooling, and control are optimized both locally and jointly.

### 1.1 Core notation

| Symbol | Meaning |
|---|---|
| $A$ | Candidate runtime composed of four mutable subsystems. |
| $g^{\mathrm{top}}, g^{\mathrm{mem}}, g^{\mathrm{tool}}, g^{\mathrm{ctl}}$ | Mutable code governing topology, memory, tooling, and control. |
| $\mathcal{H}$ | Fixed shell: storage, verifiers, sandboxes, adapters, safety guards, and logging. |
| $x$ | Task instance. |
| $r$ | Random seed used in repeated evaluation. |
| $y_{x,r}(A)$ | Final artifact produced by runtime $A$ on task $x$ under seed $r$. |
| $\tau_{x,r}(A)$ | Full execution trace. |
| $\begin{array}{c}C_{x,r}(A),\,L_{x,r}(A),\\ H_{x,r}(A)\end{array}$ | Cost, latency, and operational faults. |
| $V_{x,r}(A)$ | Benchmark-specific verifier score in $[0,1]$. |
| $u_{x,r}(A)$ | Utility of one task-seed run after cost, latency, and fault penalties. |
| $s_x(A), \rho_x(A), \chi_x(A)$ | Primary score, shrinkage-robust score, and tail-risk diagnostic for task $x$. |
| $\mathcal{F}_{\mathrm{obj}}$ | Archive objective set. |
| $S$ | Mutation scope, a non-empty subset of $\set{\mathrm{top},\mathrm{mem},\mathrm{tool},\mathrm{ctl}}$. |
| $I_f$ | Archive island associated with objective $f$. |
| $z_t$ | Runtime state at inner-loop step $t$. |

## 2. Fixed Shell, Mutable Genotype, and Mandatory Schemas

### 2.1 Candidate runtime and fixed shell

A candidate runtime is

$$
A = \paren{g^{\mathrm{top}}, g^{\mathrm{mem}}, g^{\mathrm{tool}}, g^{\mathrm{ctl}}}.
$$

*Variables.* $g^{\mathrm{top}}$ selects and orchestrates agents; $g^{\mathrm{mem}}$ governs short-term and long-term memory; $g^{\mathrm{tool}}$ governs discovery, synthesis, validation, and dispatch of tools; $g^{\mathrm{ctl}}$ governs model allocation, verification, stopping, and mutation-surface scoring.

The fixed shell $\mathcal{H}$ is immutable during search. It contains the canonical agent pool, short-term graph store, long-term graph store, benchmark adapters, verifiers, safety guards, sandbox manager, environment cache, trace logging, token accounting, wall-clock accounting, and patch parser or applier. Search operates over $A$, never over $\mathcal{H}$.

### 2.2 Mutable methods

The mutable surface is intentionally narrow.

**Topology:** `score_agent`, `select_mode`, `propose_children`, `select_workers`, `assign_scope`, `merge_ensemble`, `make_checkpoint`.

**Memory:** `select_spans_for_compaction`, `summarize_span`, `retrieve_long_term`, `score_memory_unit`, `should_promote`, `dedup_candidates`, `upsert_memory`.

**Tooling:** `rank_categories`, `rank_tools`, `should_create_tool`, `propose_tool_spec`, `validate_tool`, `promote_tool`, `dispatch_tool`.

**Control:** `assign_model`, `request_checks`, `stop_policy`, `score_interface_scope`, `update_scope_credit`.

Any helper routine called only by these methods may mutate. Benchmark graders, storage backends, sandbox boundaries, benchmark prompts, safety prompts, environment caches, and graph query engines may not.

### 2.3 Mandatory schemas

Implementations may add fields, but they may not delete any field listed below.

| Object | Required fields |
|---|---|
| `AgentTemplate` | `agent_id`, `description`, `capability_set`, `symbol_set`, `default_tool_scope`, `success_stats`, `staleness_clock`, `model_policy_tag`. |
| `ChildSpec` | `child_id`, `role`, `instruction`, `tool_scope`, `model_class`, `required_capabilities`, `required_permissions`, `dependency_ids`, `comm_mode`, `resume_policy`, `init_summary`. |
| `ToolSpec` | `name`, `category_path`, `signature`, `description`, `runtime`, `deps`, `permissions`, `tests`, `backgroundable`, `state_schema`, `source_digest`, `build_cmd`, `run_cmd`, `timeout_s`, `determinism_class`. |
| `SummaryRecord` | `objective`, `evidence`, `artifacts`, `unresolved`, `open_handles`, `next_actions`, `symbols`, `verifier_state`, `provenance`. |
| `Checkpoint` | `summary`, `artifact_refs`, `open_handles`, `unresolved_goals`, `budget_state`, `verifier_state`, `resume_constraints`. |
| `AsyncHandle` | `handle_id`, `tool_name`, `sandbox_hash`, `working_directory`, `launch_time`, `timeout`, `stdout_path`, `stderr_path`, `state`, `artifact_refs`. |
| `MemoryNode` | `node_id`, `type`, `label`, `content`, `embedding`, `symbol_set`, `file_paths`, `source_task_id`, `verifier_support`, `timestamps`, `provenance`, `tombstoned`. |
| `ArchiveEntry` | `code_hash`, `runtime_hash`, `scores`, `behavior_bin`, `scope_tag`, `complexity_bucket`, `mutable_loc`, `trace_refs`. |

### 2.4 Graph contracts and invariants

Short-term memory is an append-only directed graph.

**Mandatory short-term node types:** `AgentRun`, `Event`, `Summary`, `Artifact`, `RawBlob`, `OpenHandle`, `VerifierEvidence`.

**Mandatory short-term edges:** `CALLS_AGENT`, `EMITS`, `SUMMARIZES`, `PRODUCES`, `BACKLINKS_TO`, `WAITS_ON`, `CONTINUES_FROM`, `VALIDATED_BY`.

Long-term memory stores reusable abstractions rather than transcripts.

**Mandatory long-term node types:** `Symbol`, `File`, `Query`, `Answer`, `ToolFailure`, `FixPattern`, `TaskNote`, `Procedure`, `EnvironmentFingerprint`, `ArtifactSignature`.

The following invariants are mandatory:

1. The canonical stored agent is never executed directly; every invocation clones the stored object and discards the clone after completion.
2. Horizontal workers share only the append-only board, with locks and per-worker read cursors; they do not share mutable short-term state.
3. Message-board state and open-handle tables must survive compaction and resume.
4. Short-term memory is append-only except for summary replacement with raw-output reachability preserved through backlinks.
5. Long-term memory resets per evaluation unit unless transfer is explicitly scored.
6. Category-first tool discovery is mandatory; loading the entire tool tree into prompt context is forbidden.
7. Sandbox reuse must be content-addressed.
8. Exact symbol and path matches dominate embedding similarity in retrieval and deduplication.
9. Merge order for worker outputs must be deterministic.
10. Validation and held-out traces may never appear in mutation prompts.

## 3. Runtime State, Evaluation Unit, and Hard Invalidation

### 3.1 Runtime state machine

A task-time run of runtime $A$ on task $x$ with seed $r$ is a state machine

$$
z_{t+1} = T_A\paren{z_t, x, \omega_t}.
$$

*Variables.* $z_t$ is runtime state at step $t$; $T_A$ is the transition rule induced by runtime $A$; $x$ is the current task; $\omega_t$ collects admissible stochasticity such as model sampling under the fixed seed.

State $z_t$ contains the active agent queue $Q_t$, short-term execution graph $G_t^{S}$, long-term graph $G_t^{L}$, visible tool-registry slice $R_t$, budget state $b_t$, open async handles $o_t$, verifier evidence state $e_t$, and current confidence or unresolved-goal statistics.

The initial state contains one root agent, empty short-term memory, a fresh open-handle table, the canonical tool registry, and long-term memory reset according to the evaluation protocol. A run emits final artifact $y_{x,r}(A)$, execution trace $\tau_{x,r}(A)$, total cost $C_{x,r}(A)$, latency $L_{x,r}(A)$, and operational faults $H_{x,r}(A)$.

### 3.2 Evaluation unit

An evaluation unit is either:

1. a single task $x$, when transfer is not itself scored; or
2. an ordered episode $e=(x_1,\dots,x_m)$, when transfer is explicitly part of the benchmark objective.

Dynamic agents, dynamic tools, and short-term memory always reset between tasks. Long-term memory resets between independent tasks unless transfer is explicitly scored. Candidate-specific learned predictor parameters do not leak validation or test information back into mutation.

### 3.3 Hard invalidation

A run is immediately invalid on task $x$ if any of the following occurs:

- benchmark adapter mutated or bypassed,
- safety boundary violated,
- forbidden filesystem or network access,
- canonical stored agent executed directly instead of clone-on-run,
- open-handle table becomes inconsistent,
- short-term compaction destroys raw-output reachability,
- long-term memory carries across tasks when transfer is not explicitly scored.

Invalid runs receive

$$
V_{x,r}(A)=0
$$

*Variables.* $V_{x,r}(A)$ is the benchmark verifier score for task $x$, seed $r$, and runtime $A$. A hard-invalid run is forced to zero verifier score for that run and cannot be inserted into the archive.

A candidate that triggers hard invalidation at Stage~0, 1, or 2 is rejected immediately and contributes only to failure statistics.

## 4. Evaluation Setting, Objectives, and Statistical Protocol

### 4.1 Task partition and mutator isolation

The scored training tasks are partitioned as

$$
X_{\mathrm{train}} = X_{\mathrm{top}} \cup X_{\mathrm{mem}} \cup X_{\mathrm{tool}} \cup X_{\mathrm{e2e}}.
$$

*Variables.* $X_{\mathrm{train}}$ is the training suite. $X_{\mathrm{top}}$, $X_{\mathrm{mem}}$, $X_{\mathrm{tool}}$, and $X_{\mathrm{e2e}}$ are topology, memory, tooling, and end-to-end task families.

A separate validation set $X_{\mathrm{val}}$ is used only to choose leaders and trigger curriculum advancement. A disjoint test set $X_{\mathrm{test}}$ is evaluated once at the end. No trace, failure message, or grader output from $X_{\mathrm{val}}$ or $X_{\mathrm{test}}$ may enter a mutation prompt.

### 4.2 Verifier score and per-seed utility

For task $x$ and seed $r$, the benchmark-specific verifier returns

$$
V_{x,r}(A) = V_x\paren{y_{x,r}(A), \tau_{x,r}(A)} \in [0,1].
$$

*Variables.* $V_x(\cdot)$ is the task-specific verifier; $y_{x,r}(A)$ is the final artifact; $\tau_{x,r}(A)$ is the full execution trace.

All parent-child comparisons use common random numbers: if parent and child are both evaluated on task $x$, they must use the same seed set.

Reference cost and latency scales are task-specific. Define $C_{0,x}$ and $L_{0,x}$ as the median baseline cost and latency on task $x$ over the initial archive and default seed set:

$$
\begin{align}
C_{0,x} &= \max\set{1,\ \median_r\, C_{x,r}(A_0)}, \\
L_{0,x} &= \max\set{1,\ \median_r\, L_{x,r}(A_0)}.
\end{align}
$$

*Variables.* $A_0$ is the baseline runtime. $C_{0,x}$ and $L_{0,x}$ are task-specific normalization constants derived from the baseline.

Per-seed utility is

$$
\begin{aligned}
u_{x,r}(A) =\;& V_{x,r}(A)
- \lambda_C \log\!\paren{1+\frac{C_{x,r}(A)}{C_{0,x}}}
- \lambda_L \log\!\paren{1+\frac{L_{x,r}(A)}{L_{0,x}}} \\
&\; - \lambda_H H_{x,r}(A).
\end{aligned}
$$

*Variables.* $u_{x,r}(A)$ is the task-seed utility after cost, latency, and fault penalties. $\lambda_C$, $\lambda_L$, and $\lambda_H$ are penalty weights.

The repeated-seed primary score is

$$
s_x(A) = \frac{1}{R}\sum_{r=1}^{R} u_{x,r}(A).
$$

*Variables.* $s_x(A)$ is the mean utility of runtime $A$ on task $x$ over $R$ repeated seeds.

### 4.3 Robustness and tail risk

With low seed count, raw sample variance is noisy. Agintor therefore uses shrinkage:

$$
\hat{\sigma}_x^2(A)
=
(1-\eta_{\sigma})\, \Var_r\paren{u_{x,r}(A)}
+
\eta_{\sigma}\, \sigma^2_{f(x),\mathrm{prior}}.
$$

*Variables.* $\hat{\sigma}_x^2(A)$ is the shrinkage variance estimate of task utility; $\eta_{\sigma}$ controls shrinkage strength; $\sigma^2_{f(x),\mathrm{prior}}$ is the prior variance for the family containing task $x$.

The robustness-adjusted task score is

$$
\rho_x(A)
=
s_x(A)
-
\kappa_b \hat{\sigma}_x(A)
-
\kappa_u \frac{\hat{\sigma}_x(A)}{\sqrt{R}}.
$$

*Variables.* $\rho_x(A)$ is the search-time robustness score. $\kappa_b$ penalizes brittleness and $\kappa_u$ penalizes statistical uncertainty from low seed count.

In addition, the runtime tracks a tail-risk diagnostic

$$
\chi_x(A)=\cvar_{\alpha}\!\paren{\set{u_{x,r}(A)}_{r=1}^{R}},
$$

*Variables.* $\chi_x(A)$ is the lower-tail conditional value at risk of task utility. It is used for validation and final champion selection, not for archive insertion. $\alpha$ is the tail fraction.

For finite $R$, if $u_{x,(1)}\le \dots \le u_{x,(R)}$ are the sorted utilities and $k=\max\set{1,\lceil \alpha R\rceil}$, then

$$
\cvar_{\alpha} = \frac{1}{k}\sum_{i=1}^{k} u_{x,(i)}.
$$

*Variables.* $u_{x,(i)}$ is the $i$th order statistic after sorting utilities in ascending order. $k$ is the number of worst-seed utilities averaged into the tail statistic.

### 4.4 Family and global scores

Family averages are

$$
\bar{s}_f(A)=\frac{1}{|X_f|}\sum_{x\in X_f} s_x(A),
\qquad
\bar{\rho}_f(A)=\frac{1}{|X_f|}\sum_{x\in X_f} \rho_x(A).
$$

*Variables.* $X_f$ is the set of training tasks in family $f$. $\bar{s}_f(A)$ and $\bar{\rho}_f(A)$ are family means of primary and robustness-adjusted scores.

Global means are hierarchically weighted by family:

$$
\bar{s}(A)=\sum_{f}\omega_f \bar{s}_f(A),
\qquad
\bar{\rho}(A)=\sum_{f}\omega_f \bar{\rho}_f(A).
$$

*Variables.* $\omega_f$ is the family weight; by default the four major families receive equal weight so larger families do not dominate search solely by cardinality.

The archive objective set is

$$
\mathcal{F}_{\mathrm{obj}}
=
\set{s_x : x\in X_{\mathrm{train}}}
\cup
\set{\bar{s}_{\mathrm{top}}, \bar{s}_{\mathrm{mem}}, \bar{s}_{\mathrm{tool}}, \bar{s}_{\mathrm{e2e}}, \bar{\rho}_{\mathrm{top}}, \bar{\rho}_{\mathrm{mem}}, \bar{\rho}_{\mathrm{tool}}, \bar{\rho}_{\mathrm{e2e}}, \bar{s}, \bar{\rho}}.
$$

*Variables.* $\mathcal{F}_{\mathrm{obj}}$ contains single-task specialists, family generalists, family-robust variants, and global objectives.

Operational faults count only non-fatal runtime failures, including hallucinated tool invocations corrected by fallback, broken checkpoints missing non-critical fields, async timeouts recovered by retry, and malformed memory writes rejected by validators. Safety violations do not increment $H_{x,r}$; they invalidate the run.

## 5. Predictor Families, Uncertainty, and Online Calibration

Every hatted quantity belongs to one of three predictor types and is implemented inside a decision-family model bank.

### 5.1 Base predictor types

For decision family $d$ and candidate action $a$ in runtime state $s$, define a deterministic feature map

$$
\phi_d(s,a)\in \R^{m_d}.
$$

*Variables.* $d$ indexes a decision family such as mode selection, child spawning, compaction, retrieval, tool reuse, tool creation, model choice, or stopping. $m_d$ is the feature dimension for family $d$.

Probability predictors use logistic regression with isotonic calibration:

$$
\hat{p}_d(s,a)=\clip\paren{\mathrm{Iso}\paren{\sigmoid\paren{w_d^{\top}\phi_d(s,a)}},\ p_{\min},\ p_{\max}}.
$$

*Variables.* $\hat{p}_d$ is the calibrated probability estimate for family $d$. $\mathrm{Iso}(\cdot)$ is the isotonic calibration map. $p_{\min}$ and $p_{\max}$ clip pathological probabilities away from exactly $0$ and $1$.

Positive scalar predictors use log-linear Huber regression:

$$
\hat{q}_d(s,a)=\exp\paren{u_d^{\top}\phi_d(s,a)}.
$$

*Variables.* $\hat{q}_d$ is a positive-valued prediction such as cost, latency, or compaction time. $u_d$ is the regression parameter vector for family $d$.

Ranking scores use normalized linear mixtures:

$$
\hat{r}_d(s,a)=\sum_i \alpha_i \tilde{\phi}_{d,i}(s,a),
$$

*Variables.* $\tilde{\phi}_{d,i}(s,a)$ is feature $i$ normalized to $[0,1]$ within the current candidate set, and $\alpha_i$ is its ranking weight.

### 5.2 Decision-family model bank

For every family $d$, maintain bootstrapped ensembles for the relevant probability and positive-scalar predictors. Let $\mu[\cdot]$ and $\sigma[\cdot]$ denote ensemble mean and standard deviation. Family utility is

$$
\begin{aligned}
U_d(a\mid s)=\;&
\mu[\hat{p}_d]
-
\lambda_T^{(d)} \log\!\paren{1+\frac{\mu[\hat{T}_d]}{T_0^{(d)}}}
-
\lambda_L^{(d)} \log\!\paren{1+\frac{\mu[\hat{L}_d]}{L_0^{(d)}}} \\
&\;
-
\lambda_F^{(d)} \mu[\hat{F}_d]
+
\lambda_Q^{(d)} \mu[\hat{Q}_d].
\end{aligned}
$$

*Variables.* $U_d(a\mid s)$ is the scalar utility of action $a$ under decision family $d$. $\hat{T}_d$, $\hat{L}_d$, $\hat{F}_d$, and $\hat{Q}_d$ are predicted token cost, latency, fault probability, and family-specific auxiliary value.

Conservative and optimistic utilities are

$$
U_d^{-}(a\mid s)=U_d(a\mid s)-\beta_d \sigma\bracks{U_d(a\mid s)},
\qquad
U_d^{+}(a\mid s)=U_d(a\mid s)+\beta_d \sigma\bracks{U_d(a\mid s)}.
$$

*Variables.* $U_d^{-}$ is the conservative utility used for irreversible actions and hard gating. $U_d^{+}$ is the optimistic utility used for exploration actions such as tool creation or ensemble widening. $\beta_d$ is the uncertainty multiplier for family $d$.

### 5.3 Feature groups, labels, and update protocol

Topology features include task embedding, symbolic seeds, required capabilities, permission requirements, unresolved critical count, context saturation, remaining budget fractions, candidate-agent history, tool coverage gap, and expected fanout. Memory features include span age, token length, artifact count, unresolved items, handle count, node type match, graph distance from symbolic seeds, verifier support, provenance quality, recency, and staleness. Tooling features include category similarity, signature fit, dependency depth, permission risk, cold-start cost, cache-hit probability, historical pass rate, and build-test cost. Control features include remaining budget fractions, irreversibility flags, current confidence, presence of exact verifiers, model tier, and unresolved-item severity.

Labels are logged directly from traces. A topology action succeeds if its child or worker contributes an artifact later accepted by its parent or passes its local verifier. A compaction action succeeds if later steps do not require raw-transcript fallback and no evidence or handle reachability is lost. A retrieval action succeeds if the retrieved node is consumed later and is not contradicted by a verifier. Tool reuse succeeds if the selected tool executes and passes designated checks. Tool creation succeeds if the synthesized tool passes validation and is successfully reused. A model choice succeeds if the chosen model completes its action without forced escalation. A verification action succeeds if it changes a downstream decision or confirms a final artifact. A stop action succeeds if stopping yields a verifier-positive terminal artifact with no unresolved critical items.

Predictors are retrained whenever 50 fully evaluated children or 10 accepted elites accumulate since the previous update. Calibration uses the most recent 200 labeled examples per task family. If fewer than 100 examples are available for a family, the runtime falls back to the default heuristic weights in the relevant subsystem section. Surrogates are frozen during every parent-child comparison.

## 6. Archive Design, Diversity Descriptors, and Outer-Loop Controller

### 6.1 Archive cell key

Agintor uses an objective-conditioned quality-diversity archive. Each cell key is

$$
k(A,f)=\paren{f,\ q(A),\ b(A),\ S_{\mathrm{last}}(A),\ c_{\mathrm{bin}}(A)}.
$$

*Variables.* $k(A,f)$ is the archive cell key for runtime $A$ under objective $f$. $q(A)$ is the interface-difference bitmask relative to baseline, $b(A)$ is the behavior descriptor, $S_{\mathrm{last}}(A)$ is the scope of the last accepted mutation, and $c_{\mathrm{bin}}(A)$ is the complexity bucket.

The behavior descriptor is

$$
b(A)=\paren{d_{\mathrm{mode}}, d_{\mathrm{tool}}, d_{\mathrm{mem}}, d_{\mathrm{ver}}}.
$$

*Variables.* $d_{\mathrm{mode}}$ is the dominant solve mode in $\set{\mathrm{single},\mathrm{vertical},\mathrm{horizontal},\mathrm{mixed}}$. $d_{\mathrm{tool}}$, $d_{\mathrm{mem}}$, and $d_{\mathrm{ver}}$ are trinary bins for created-tool rate, promotion density, and checks-per-task.

The complexity bucket is the insertion-time quartile of mutable AST-node count relative to the current archive; mutable changed LOC is retained as a deterministic tie-breaker.

### 6.2 Parent selection and elite replacement

Within objective island $I_f$, normalize the objective:

$$
\tilde{f}(A)=\frac{f(A)-\mu_f}{\sigma_f+\varepsilon}.
$$

*Variables.* $\tilde{f}(A)$ is the within-island normalized objective value. $\mu_f$ and $\sigma_f$ are the mean and standard deviation of objective $f$ over island $I_f$. $\varepsilon$ is a stability constant.

Parent selection uses inverse temperature $\beta_{\mathrm{sel}}$:

$$
P(A\mid f)=\frac{\exp\paren{\beta_{\mathrm{sel}}\tilde{f}(A)}}{\sum_{B\in I_f}\exp\paren{\beta_{\mathrm{sel}}\tilde{f}(B)}}.
$$

*Variables.* $P(A\mid f)$ is the probability of selecting runtime $A$ from island $I_f$ under objective $f$. Larger $\beta_{\mathrm{sel}}$ sharpens selection.

Within a cell, child $A'$ replaces elite $A$ under objective $f$ iff

$$
A' \succ_f A
\iff
\paren{f(A')>f(A)+\delta_f}
\;\vee\;
\paren{|f(A')-f(A)|\le \delta_f\ \land\ \ell(A')<\ell(A)}.
$$

*Variables.* $\delta_f$ is the score tolerance for objective $f$. $\ell(A)$ measures mutable-code complexity using edited AST nodes, with changed LOC as the secondary tie-breaker.

### 6.3 Scope scheduler and credit assignment

Let $\mathcal{S}_t$ be the admissible mutation scopes at outer-search step $t$. Maintain:

- singleton counterfactual credits $a_t(i)$ for each interface $i$,
- pairwise interaction credits $b_t(i,j)$ for each unordered pair $i<j$,
- objective-conditioned scope credit $c_{f,t}(S)$ for every admissible scope $S$,
- staleness $\mathrm{stagn}_t(S)$, need $\mathrm{need}_t(S)$, and hard-failure rate $\mathrm{hardfail}_t(S)$.

Aggregate global scope credit is

$$
c_t(S)=\sum_{i\in S} a_t(i)+\sum_{i<j,\ i,j\in S} b_t(i,j).
$$

*Variables.* $c_t(S)$ is the global scope credit for mutation scope $S$, obtained by summing singleton and pairwise interaction credits.

Scope utility for objective $f$ is

$$
u_t(S\mid f)
=
\omega_1 c_t(S)
+\omega_2 c_{f,t}(S)
+\omega_3 \mathrm{stagn}_t(S)
+\omega_4 \mathrm{need}_t(S)
-\omega_5 \mathrm{hardfail}_t(S)
-\omega_6 |S|.
$$

*Variables.* $u_t(S\mid f)$ is the scope utility for scope $S$ when optimizing objective $f$. $\omega_i$ are scheduler weights.

Scopes are sampled by softmax:

$$
P(S_t=S)=\frac{\exp\paren{\beta_{\mathrm{scope}} u_t(S\mid f)}}{\sum_{S'\in \mathcal{S}_t}\exp\paren{\beta_{\mathrm{scope}} u_t(S'\mid f)}}.
$$

*Variables.* $S_t$ is the mutation scope selected at outer step $t$. $\beta_{\mathrm{scope}}$ is the scope-selection inverse temperature.

Credit is updated for every fully evaluated child, whether or not it enters the archive. For child $A'_t$ derived from parent $A_t$ with exact touched scope $S$,

$$
\Delta_f(A'_t,A_t)
=
\frac{\sum_{x\in X_{\mathrm{train}}} w_x \paren{s_x(A'_t)-s_x(A_t)}}{\max\paren{1,|S|}}.
$$

*Variables.* $\Delta_f(A'_t,A_t)$ is the objective-conditioned credit signal assigned to scope $S$. $w_x$ are task weights, uniform within family by default.

Then

$$
c_{f,t+1}(S)=(1-\xi_f)c_{f,t}(S)+\xi_f \Delta_f(A'_t,A_t)\,\ind\bracks{S=\mathrm{touch}(A_t,A'_t)}.
$$

*Variables.* $\xi_f$ is the exponential-moving-average update rate for objective-conditioned scope credit. $\mathrm{touch}(A_t,A'_t)$ is the exact mutated interface set.

For accepted children only, compute counterfactual singleton and pair contributions on a fixed attribution proxy suite $P_{\mathrm{att}}(S)$. Let $g_S(\cdot)$ be the mean proxy score on that suite. For interface $i\in S$, let $A'_{-i}$ revert only the interface-$i$ hunks, and for pair $\{i,j\}\subseteq S$, let $A'_{-\{i,j\}}$ revert both. Then

$$
\begin{align}
\Delta_i &= g_S(A')-g_S(A'_{-i}), \\
\Delta_{ij} &= g_S(A')-g_S(A'_{-i})-g_S(A'_{-j})+g_S(A'_{-\{i,j\}}).
\end{align}
$$

*Variables.* $\Delta_i$ and $\Delta_{ij}$ are singleton and pairwise counterfactual contributions of accepted child $A'$.

Update singleton and pair credits by

$$
\begin{align}
a_{t+1}(i) &= (1-\xi_a)a_t(i)+\xi_a \Delta_i, \\
b_{t+1}(i,j) &= (1-\xi_b)b_t(i,j)+\xi_b \Delta_{ij}.
\end{align}
$$

*Variables.* $\xi_a$ and $\xi_b$ are the update rates for singleton and pairwise interaction credits.

### 6.4 Interface-wise crossover and outer loop

Crossover is allowed only at whole-method granularity on disjoint mutable symbols. A valid crossover selects at most one donor per mutable method, rejects overlapping symbol edits, reparses the merged file set, and passes the same staged evaluation used for ordinary mutations.

**Algorithm 1. Outer evolutionary search**

1. Initialize the archive with one baseline runtime and 4--8 subsystem-local handwritten variants.
2. Sample an objective $f \in \mathcal{F}_{\mathrm{obj}}$.
3. Sample a mutation scope $S_t$ from the scope scheduler.
4. Sample a parent from island $I_f$; optionally apply whole-method crossover.
5. Build a mutation prompt from the mutable files, contracts, predictor summaries, recent failing train-set traces, and high-performing exemplars.
6. Request exact SEARCH/REPLACE patches.
7. Apply the patch; reject immediately on parser, contract, or immutability failure.
8. Run staged evaluation.
9. Update hard-failure statistics.
10. If full evaluation completed, update scope credit.
11. Insert the child into every improved archive cell.
12. Periodically score leaders on validation and advance the curriculum when the trigger in Section~\ref{sec:curriculum} fires.
13. At the end, select leaders on validation and evaluate them once on the held-out test set.

## 7. Evolution of Self-Generating Topology

Topology evolves the runtime logic that decides whether a task is solved by a single agent, by vertical decomposition into specialized children, or by a small horizontal ensemble.

### 7.1 Agent reuse versus creation

Let $q_x$ be the task representation, $T_x$ the predicted tool footprint, $\Gamma_x$ the required capability multiset, and $d_a$, $T_a$, $\Gamma_a$ the description, canonical tool scope, and capability multiset of stored agent $a$. Reuse is scored by

$$
\begin{aligned}
r_A(a\mid x)=\;&
\alpha_1 \mathrm{sim}(d_a,q_x)
+\alpha_2 \mathrm{overlap}(T_a,T_x)
+\alpha_3 J(\Gamma_a,\Gamma_x)
+\alpha_4 \mathrm{succ}(a,f(x)) \\
&\; + \alpha_5 \mathrm{reusefit}(a,x)
-\alpha_6 \mathrm{ctx}(a)
-\alpha_7 \mathrm{stale}(a)
-\alpha_8 \mathrm{permgap}(a,x).
\end{aligned}
$$

*Variables.* $r_A(a\mid x)$ is the reuse score of stored agent $a$ for task $x$. $J(\cdot,\cdot)$ is Jaccard overlap on capability multisets. $\mathrm{succ}(a,f(x))$ is family-conditioned historical success. $\mathrm{ctx}(a)$ is prompt overhead and $\mathrm{permgap}(a,x)$ penalizes permission mismatch.

Create a new child only when

$$
\max_a r_A(a\mid x)<\theta_{\mathrm{create}}
\quad\text{or}\quad
\mathrm{capgap}(x,a^*)>\eta_{\mathrm{gap}},
$$

*Variables.* $\theta_{\mathrm{create}}$ is the reuse-versus-create threshold. $\mathrm{capgap}(x,a^*)$ is the uncovered capability mass of the best reusable agent $a^*$.

### 7.2 Mode selection and child gating

Given runtime state $z_t$ and task $x$, choose

$$
c_t^*(x)=\argmax_{c\in\set{\mathrm{single},\mathrm{vertical},\mathrm{horizontal}}}
\bracks{\hat{p}_{\mathrm{solve}}(c\mid z_t,x)-\lambda_C \hat{C}(c\mid z_t,x)-\lambda_L \hat{L}(c\mid z_t,x)-\lambda_Q \hat{Q}(c\mid z_t,x)}.
$$

*Variables.* $c_t^*(x)$ is the selected topology mode. $\hat{p}_{\mathrm{solve}}$, $\hat{C}$, $\hat{L}$, and $\hat{Q}$ predict solve probability, cost, latency, and coordination risk.

For candidate child specification $z_j$,

$$
\Delta_j(x)
=
\hat{p}_{\mathrm{solve}}(z_j\mid x,z_t)
-
\hat{p}_{\mathrm{solve}}(\varnothing\mid x,z_t)
-
\lambda_{\mathrm{spawn}}
-
\lambda_{\mathrm{coord}}\, \mathrm{fanout}(z_j)
-
\lambda_{\mathrm{dep}}\, \mathrm{unmet}(z_j).
$$

*Variables.* $\Delta_j(x)$ is the marginal value of spawning child $j$. $\varnothing$ denotes not spawning any extra child. $\mathrm{fanout}(z_j)$ is the coordination burden and $\mathrm{unmet}(z_j)$ is the number or weighted mass of unresolved dependencies.

Spawn the child only if $\Delta_j(x)>0$. Children are ordered. Each child receives an independent short-term-memory root plus a checkpoint policy of summary + handles + artifacts.

### 7.3 Joint tool-scope assignment

Let $R^{\mathrm{cand}}_j$ be the candidate tools for child $j$, after category-first discovery. The selected scope is

$$
T_j^*
=
\argmax_{T\subseteq R^{\mathrm{cand}}_j,\ |T|\le 12}
\bracks{
\mathrm{cov}(T,z_j)
-
\lambda_{\mathrm{size}} |T|
-
\lambda_{\mathrm{cf}} \mathrm{conflict}(T)
-
\lambda_{\mathrm{cold}} \sum_{\tau\in T}\mathrm{coldstart}(\tau)
}.
$$

*Variables.* $T_j^*$ is the assigned tool scope for child $j$. $\mathrm{cov}(T,z_j)$ measures how well tool set $T$ covers child needs. $\mathrm{conflict}(T)$ penalizes overlapping or incompatible tools.

This optimization is solved greedily over the top 12 candidate tools returned by category-first discovery.

### 7.4 Horizontal worker subset selection

Let $W_{\mathrm{cand}}$ be the candidate workers proposed for the same task. Select the worker subset directly:

$$
\begin{aligned}
W^* = \argmax_{W\subseteq W_{\mathrm{cand}},\ 1\le |W|\le K_{\max}}
\Biggl[
&1-\prod_{j\in W}(1-\hat{p}_j)
-\lambda_D \frac{2}{\max\paren{1,|W|(|W|-1)}} \sum_{i<j,\ i,j\in W}\mathrm{sim}(p_i,p_j) \\
&-\lambda_K |W|
-\lambda_T \sum_{j\in W}\frac{\hat{T}_j}{T_0}
-\lambda_L \max_{j\in W}\frac{\hat{L}_j}{L_0}
\Biggr].
\end{aligned}
$$

*Variables.* $W^*$ is the selected worker subset. $\hat{p}_j$, $\hat{T}_j$, and $\hat{L}_j$ are predicted solve probability, token cost, and latency for worker $j$. $\mathrm{sim}(p_i,p_j)$ measures plan similarity. $K_{\max}$ is the maximum ensemble size.

The optimization is implemented by greedy forward selection up to $K_{\max}=3$.

### 7.5 Deterministic merge policy and topology runtime

Worker outputs are merged in deterministic order: verified artifacts first, then by verifier support score, then by predicted solve probability, then by unresolved-critical-count ascending, and finally by lexicographic worker id.

**Algorithm 2. Runtime topology control**

1. Search the stored-agent pool before any child creation; create only when reuse falls below $\theta_{\mathrm{create}}$ or capability gap remains too large.
2. Estimate the best control mode under the mode objective using task difficulty, context saturation, tool cold-start cost, and verifier hints.
3. If vertical, propose ordered child specifications, assign each a minimal tool scope, attach an independent short-term root, and spawn only positive-$\Delta_j$ children.
4. If horizontal, create at most $K_{\max}$ materially different workers; require plan diversity rather than superficial prompt perturbation.
5. Persist tool calls, child summaries, verifier evidence, and open async handles into the short-term graph; expose only compressed summaries to the parent.
6. Resume children from the latest checkpoint summary, unresolved goals, open handles, and artifact references rather than from raw transcripts.

## 8. Evolution of Hierarchical Memory

### 8.1 Short-term and long-term graphs

Short-term memory is an append-only directed graph with the node and edge types specified in Section~2. Long-term memory stores reusable abstractions rather than transcripts. Every long-term node must expose base fields for type, label, content, embedding, exact symbol set, file paths, source task id, verifier support, timestamps, and provenance.

### 8.2 Compaction as global budget control

For candidate span $h_i$ and action $a\in\set{\mathrm{keep},\mathrm{summarize},\mathrm{checkpoint}}$,

$$
\mathrm{score}_{\mathrm{cmp}}(h_i,a)
=
\hat{R}_{\mathrm{ret}}(a\mid h_i)
+
\lambda_{\mathrm{tok}} \Delta \mathrm{tok}(h_i,a)
-
\lambda_{\mathrm{loss}} \hat{L}_{\mathrm{info}}(a\mid h_i)
-
\lambda_{\mathrm{lat}} \hat{T}_{\mathrm{cmp}}(a\mid h_i)
-
\lambda_{\mathrm{orph}} O(h_i,a).
$$

*Variables.* $\mathrm{score}_{\mathrm{cmp}}(h_i,a)$ is the compaction score. $\Delta \mathrm{tok}(h_i,a)$ is tokens saved by action $a$ on span $h_i$. $\hat{R}_{\mathrm{ret}}$ predicts retained utility, $\hat{L}_{\mathrm{info}}$ predicts information loss, $\hat{T}_{\mathrm{cmp}}$ predicts compaction latency, and $O(h_i,a)$ penalizes orphaned raw outputs, artifact references, or async handles.

If active-history budget fraction $b_t > B_{\mathrm{hi}}$, collect all admissible $(h_i,a)$ pairs, rank them by density

$$
\mathrm{density}(h_i,a)
=
\frac{\mathrm{score}_{\mathrm{cmp}}(h_i,a)}{\max\paren{1,\Delta \mathrm{tok}(h_i,a)}},
$$

*Variables.* $\mathrm{density}(h_i,a)$ prioritizes actions that save prompt budget efficiently while preserving downstream utility.

apply the highest-density positive actions greedily, and continue until $b_t < B_{\mathrm{lo}}$. If no positive action exists but the hard context limit is exceeded, summarize oldest spans first as a last resort. Every summary must preserve the fields in `SummaryRecord`.

### 8.3 Long-term retrieval

Given query $q_x$, build the candidate set as the union of exact symbol or file-path matches, top-$M_e$ embedding neighbors, one-hop graph expansions around exact matches, and top-$M_{\ell}$ lexical matches. Exact symbol and path matches dominate embeddings. Retrieval score is

$$
\mathrm{score}_L(v\mid q_x)
=
\begin{cases}
\begin{aligned}
1 &+ \lambda_{\mathrm{path}}\, \mathrm{pathbonus}(v,q_x)
+ \lambda_{\nu}\, \mathrm{verifysupport}(v) \\
&+ \lambda_{\mathrm{prov}}\, \mathrm{provenance}(v),
\end{aligned}
& \mathrm{exactsym}(v,q_x)=1, \\[0.35em]
\begin{aligned}
&\lambda_1 \tilde{f}_{\cos}
+ \lambda_2 \tilde{f}_{\mathrm{lex}}
+ \lambda_3 \tilde{f}_{\mathrm{type}}
+ \lambda_4 \tilde{f}_{\mathrm{path}} \\
&\quad + \lambda_5 \tilde{f}_{\mathrm{rec}}
+ \lambda_6 \tilde{f}_{\nu}
+ \lambda_7 \tilde{f}_{\mathrm{prov}}
- \lambda_8 \tilde{f}_{\mathrm{stale}},
\end{aligned}
& \text{otherwise.}
\end{cases}
$$

*Variables.* $\mathrm{score}_L(v\mid q_x)$ ranks long-term memory node $v$ for query $q_x$. $\mathrm{exactsym}(v,q_x)$ detects exact symbol agreement. The $\tilde{f}$ terms are normalized cosine, lexical, type, path, recency, verifier-support, provenance, and staleness features.

Before online fitting, default normalized weights are

$$
(\lambda_1,\dots,\lambda_8)=(0.30,0.20,0.15,0.10,0.10,0.10,0.05,0.05).
$$

### 8.4 Promotion, deduplication, and writes

For candidate memory unit $u$,

$$
p_{\mathrm{prom}}(u)
=
\sigmoid\!\left(
\begin{aligned}
& w_1 n(u) + w_2 r(u) + w_3 c(u) + w_4 \nu(u) + w_5 t(u) \\
& + w_6 \mathrm{comp}(u) - w_7 d(u) - w_8 w(u) - w_9 \mathrm{contrad}(u)
\end{aligned}
\right).
$$

*Variables.* $p_{\mathrm{prom}}(u)$ is the promotion probability for unit $u$. $n(u)$ is novelty, $r(u)$ anticipated reuse, $c(u)$ artifact centrality, $\nu(u)$ verifier support, $t(u)$ task-spread potential, $\mathrm{comp}(u)$ compositional value, $d(u)$ duplicate risk, $w(u)$ write or maintenance cost, and $\mathrm{contrad}(u)$ contradiction risk.

Promote iff $p_{\mathrm{prom}}(u)\ge \theta_{\mathrm{prom}}$, and for claim-like nodes additionally require $\nu(u)\ge \eta_{\nu}$.

Deduplication is type-aware:

$$
\mathrm{merge}(u,v)
=
\ind\!\left[
\begin{aligned}
&\mathrm{type}(u)=\mathrm{type}(v) \\
&\land \Bigl(
\mathrm{primarykey}(u,v)=1
\ \vee\
\paren{\mathrm{exactsym}(u,v)\land \mathrm{namespace\_match}(u,v)} \\
&\qquad\qquad \vee\
\paren{\cos(e_u,e_v)>\theta_e \land \mathrm{jaccard}(\mathrm{tok}(u),\mathrm{tok}(v))>\theta_{\ell}}
\Bigr)
\end{aligned}
\right].
$$

*Variables.* $\mathrm{merge}(u,v)$ is the deduplication predicate for memory units $u$ and $v$. Exact symbols plus namespace or path agreement dominate. Otherwise both embedding and lexical overlap thresholds must be satisfied.

Given local neighborhood $N_u$, choose the write action by

$$
a^*(u)
=
\argmax_{a\in\set{\mathrm{merge},\mathrm{refine},\mathrm{new},\mathrm{tombstone}}}
\bracks{
\hat{G}(a\mid u,N_u)
-
\lambda_E \hat{E}(a\mid u,N_u)
-
\lambda_C \mathrm{contrad}(a\mid u,N_u)
}.
$$

*Variables.* $a^*(u)$ is the selected write action for unit $u$. $\hat{G}$ predicts utility gain, $\hat{E}$ predicts edit or maintenance cost, and $\mathrm{contrad}(a\mid u,N_u)$ measures contradiction risk in the local graph neighborhood.

## 9. Evolution of Dynamic Tooling

### 9.1 Category-first discovery and reusable-tool ranking

Let $d_c$ be category summary, $n_c$ the number of descendant leaf tools, $\mathrm{histpass}(c)$ the historical pass rate of tools in category $c$, and $\mathrm{coldstart}(c)$ its median cold-start cost. Categories are ranked by

$$
\begin{aligned}
r_c(c\mid q_x)=\;&
\alpha_1 \mathrm{sim}(d_c,q_x)
+\alpha_2 \mathrm{iface}(c,q_x)
+\alpha_3 \mathrm{histpass}(c)
+\alpha_4 \mathrm{cachehit}(c) \\
&\; - \alpha_5 \log(1+n_c)
-\alpha_6 \mathrm{coldstart}(c)
-\alpha_7 \mathrm{permrisk}(c).
\end{aligned}
$$

*Variables.* $r_c(c\mid q_x)$ is the ranking score for category $c$ under query $q_x$. $\mathrm{iface}(c,q_x)$ measures interface relevance, $\mathrm{cachehit}(c)$ measures environment reuse, and $\mathrm{permrisk}(c)$ measures permission risk.

Inspect only the top $k_c$ categories.

For reusable tool $\tau$ with metadata $m_{\tau}$,

$$
\begin{aligned}
r_{\tau}(\tau\mid q_x)=\;&
\beta_1 \mathrm{sim}(m_{\tau},q_x)
+\beta_2 \mathrm{sigmatch}(\tau,q_x)
+\beta_3 \mathrm{pass}(\tau)
+\beta_4 \mathrm{cachehit}(\tau) \\
&\; - \beta_5 \mathrm{coldstart}(\tau)
-\beta_6 \mathrm{permrisk}(\tau)
-\beta_7 \mathrm{depdepth}(\tau).
\end{aligned}
$$

*Variables.* $r_{\tau}(\tau\mid q_x)$ is the ranking score for reusable tool $\tau$. $\mathrm{sigmatch}(\tau,q_x)$ compares argument and return signatures against the current need. $\mathrm{depdepth}(\tau)$ is transitive dependency depth.

### 9.2 Build versus reuse

Creation is allowed only when new-tool value exceeds the best reusable option including expected future reuse:

$$
\mathrm{create}(q_x)
=
\ind\!\left[
\hat{G}^{\mathrm{curr}}_{\mathrm{new}}(q_x)
+
\lambda_F \hat{G}^{\mathrm{future}}_{\mathrm{new}}(q_x)
-
\max_{\tau\in R}\hat{G}_{\mathrm{reuse}}(\tau,q_x)
>
\lambda_B \hat{B}(q_x)
+
\lambda_E \hat{E}(q_x)
+
\lambda_S \hat{S}(q_x)
\right].
$$

*Variables.* $\mathrm{create}(q_x)$ is the build-versus-reuse decision for task query $q_x$. $\hat{G}^{\mathrm{curr}}_{\mathrm{new}}$ is current-task gain from a new tool, $\hat{G}^{\mathrm{future}}_{\mathrm{new}}$ is expected future reuse value, and $\hat{B}$, $\hat{E}$, and $\hat{S}$ estimate build, execution, and safety cost.

### 9.3 Tool specification, validation, promotion, and async dispatch

A synthesized tool must emit a complete `ToolSpec` and source file. Validation is mandatory: parse or syntax check, linter and import resolution, signature and schema check, smoke test, permission-boundary test, timeout test, and deterministic-output replay under a fixed seed and workspace snapshot.

A tool failing only non-critical deterministic replay may still be used as a task-local ephemeral tool if it is explicitly marked non-promotable and its outputs remain verifier-checkable. All other validation failures reject the tool.

Promotion requires both quality and reuse on distinct tasks:

$$
\mathrm{promote}(\tau)
=
\ind\!\left[
\begin{aligned}
&\mathrm{passrate}(\tau)\ge \eta_p
\ \land\
\mathrm{distinct\_task\_reuse}(\tau)\ge \eta_r \\
&\land\
\mathrm{safe}(\tau)=1
\ \land\
\mathrm{detclass}(\tau)=\mathrm{stable}
\end{aligned}
\right].
$$

*Variables.* $\mathrm{promote}(\tau)$ decides whether tool $\tau$ enters the reusable registry. $\eta_p$ and $\eta_r$ are promotion thresholds for pass rate and distinct-task reuse. Only stable tools may be promoted.

Environment reuse must be content-based:

$$
h(\tau)=H\!\left(
\begin{aligned}
&\mathrm{source\_digest}(\tau),\ \mathrm{runtime}(\tau),\ \mathrm{deps}(\tau),\ \mathrm{permissions}(\tau),\\
&\mathrm{base\_image\_digest}(\tau),\ \mathrm{compiler\_flags}(\tau),\ \mathrm{mount\_spec}(\tau),\ \mathrm{test\_digest}(\tau)
\end{aligned}
\right).
$$

*Variables.* $h(\tau)$ is the deterministic sandbox hash for tool $\tau$. It includes code identity, runtime, dependencies, permissions, base image, compilation flags, mount specification, and tests.

Dispatch chooses sync versus async using

$$
\mathrm{async}(\tau,x)=\ind\bracks{\hat{L}_{\tau}(x)>t_{\mathrm{slice}} \ \vee\ \mathrm{backgroundable}(\tau)=1}.
$$

*Variables.* $\mathrm{async}(\tau,x)$ is the asynchronous-dispatch predicate for tool $\tau$ on task $x$. $t_{\mathrm{slice}}$ is the synchronous time slice.

Background jobs return stable handles with mandatory fields for handle id, tool name, sandbox hash, working directory, launch time, timeout, stdout path, stderr path, state, and artifact references.

**Algorithm 3. Task-time tool policy**

1. Rank categories and inspect only the top $k_c$.
2. Rank reusable tools inside inspected categories.
3. If reuse is sufficient, dispatch the best reusable tool.
4. Otherwise evaluate the build-versus-reuse gate.
5. If creation is warranted, synthesize source plus `ToolSpec`, validate, and dispatch the tool as task-local or reusable as appropriate.
6. Promote only after pass-rate, distinct-task reuse, safety, and determinism thresholds are met.
7. Store failing traces, sandbox hashes, and cached build products for future reuse decisions.

## 10. Budget, Verification, and Stopping Control

### 10.1 Budget state

The normalized budget state is

$$
b_t=
\paren{
\frac{\mathrm{cost}_t}{C_{\max}},
\frac{\mathrm{lat}_t}{L_{\max}},
\frac{\mathrm{calls}_t}{M_{\max}},
\frac{\mathrm{checks}_t}{Q_{\max}}
}.
$$

*Variables.* $b_t$ summarizes consumed cost, latency, model-call count, and checker count relative to hard maxima $C_{\max}$, $L_{\max}$, $M_{\max}$, and $Q_{\max}$. Remaining budget fractions are derived from the same state.

### 10.2 Model allocation

Model allocation is a control-surface decision. Topology proposes role and scope; control chooses the cheapest model class that still satisfies predicted solve requirements and remaining budget. For subgoal $g$,

$$
m^*(g)=
\argmax_{m\in \mathcal{M}_g}
\bracks{
\hat{p}_{\mathrm{solve}}(m\mid g)
-
\lambda_C \hat{C}(m\mid g)
-
\lambda_L \hat{L}(m\mid g)
-
\lambda_{\$} \hat{\$}(m\mid g)
-
\lambda_F \hat{p}_{\mathrm{fail}}(m\mid g)
}
$$

*Variables.* $m^*(g)$ is the selected model class for subgoal $g$. $\mathcal{M}_g$ is the set of admissible model classes. $\hat{C}$, $\hat{L}$, and $\hat{\$}$ predict token, latency, and monetary cost. $\hat{p}_{\mathrm{fail}}$ predicts operational failure probability.

subject to remaining budget and minimum confidence threshold

$$
\hat{p}_{\mathrm{solve}}(m^*(g)\mid g)\ge \pi_{\min}(g).
$$

*Variables.* $\pi_{\min}(g)$ is the minimum confidence threshold for subgoal $g$.

After two consecutive negative-improvement steps on the same unresolved subgoal, one class escalation is allowed: small to medium to large.

### 10.3 Verification request policy

Let $\mathcal{K}=\set{\mathrm{local},\mathrm{subtree},\mathrm{repo},\mathrm{benchmark}}$ denote a checker ladder ordered from cheap to expensive. For evidence package $e$ and checker $k$,

$$
\mathrm{VOI}(k\mid e)
=
\hat{p}_{\mathrm{issue}}(k\mid e)\cdot \hat{L}_{\mathrm{miss}}(k\mid e)\cdot \hat{p}_{\mathrm{flip}}(k\mid e)
-
\lambda_C \hat{C}_k(e)
-
\lambda_L \hat{L}_k(e).
$$

*Variables.* $\mathrm{VOI}(k\mid e)$ is the value of information of checker $k$ for evidence package $e$. $\hat{p}_{\mathrm{issue}}$ is the probability that the checker reveals a real issue, $\hat{L}_{\mathrm{miss}}$ is the loss of missing it, $\hat{p}_{\mathrm{flip}}$ is the probability that the issue changes a downstream decision, and $\hat{C}_k$, $\hat{L}_k$ are checker cost and latency.

Run the cheapest checker with positive value of information. Escalate only if the cheaper checker passed but uncertainty remains, the artifact is externally visible, or a parent merge depends on the child output. If an exact benchmark verifier exists for an irreversible or externally visible final artifact, run it unless a hard benchmark limit forbids it.

### 10.4 Stopping rule

Let $u^{\mathrm{best}}_t$ be the best optimistic next-step utility among admissible actions. Stop iff

$$
\mathrm{stop}_t
=
\ind\!\left[
\begin{aligned}
&\mathrm{pass}_t=1
\ \vee\
\mathrm{budget\_exhausted}_t=1 \\
&\vee\
\paren{
u^{\mathrm{best}}_t<0
\ \land\
u^{\mathrm{best}}_{t-1}<0
\ \land\
\mathrm{unresolved}_t=0
\ \land\
\mathrm{verified\_terminal}_t=1}
\end{aligned}
\right].
$$

*Variables.* $\mathrm{stop}_t$ is the stopping predicate at step $t$. $\mathrm{pass}_t$ indicates decisive verifier success. $\mathrm{budget\_exhausted}_t$ indicates a hard budget boundary. $\mathrm{unresolved}_t$ counts unresolved goals and $\mathrm{verified\_terminal}_t$ indicates that a verified terminal artifact already exists.

If no verified terminal artifact exists and all admissible actions are negative, the runtime emits best-effort output only when the benchmark explicitly allows it; otherwise it returns controlled failure.

## 11. Mutation Contract, Prompt, and Curriculum

### 11.1 Required patch format

> `<<<<<<< SEARCH`  
> `<exact source lines, up to 8 lines>`  
> `=======`  
> `<replacement lines>`  
> `>>>>>>> REPLACE`

Rules:

1. SEARCH must match exactly, character for character.
2. Only SEARCH/REPLACE blocks may be returned.
3. One to four blocks are allowed per mutation by default.
4. Total changed lines must remain local and may not exceed 60 lines.
5. Blocks touching immutable files are rejected before parsing.
6. Blocks with non-unique SEARCH matches are rejected.
7. Large rewrites are disallowed.

### 11.2 Mutation prompt contents

Every mutation prompt must include the sampled objective, mutable files only, immutable-file manifest, mutable-method contracts, predictor summaries, recent failing train-set traces, and 2--6 high-performing exemplars from the archive. Validation and test traces are forbidden.

### 11.3 Admissible scopes by phase

The three curriculum phases are defined as

$$
\mathcal{S}_{\mathrm{local}} = \set{\set{\mathrm{top}}, \set{\mathrm{mem}}, \set{\mathrm{tool}}, \set{\mathrm{ctl}}},
$$

*Variables.* $\mathcal{S}_{\mathrm{local}}$ contains all singleton mutation scopes.

$$
\mathcal{S}_{\mathrm{pair}} = \set{S\subseteq \set{\mathrm{top},\mathrm{mem},\mathrm{tool},\mathrm{ctl}} : |S|=2},
$$

*Variables.* $\mathcal{S}_{\mathrm{pair}}$ contains all six pairwise mutation scopes.

$$
\mathcal{S}_{\mathrm{joint}} = \set{S\subseteq \set{\mathrm{top},\mathrm{mem},\mathrm{tool},\mathrm{ctl}} : |S|\in \set{3,4}}.
$$

*Variables.* $\mathcal{S}_{\mathrm{joint}}$ contains all joint scopes of size three or four.

### 11.4 Curriculum schedule

The default schedule is 1200 local mutations, 600 pairwise mutations, and 300 joint mutations. A phase can end early if the advancement trigger fires:

$$
\mathrm{advance}(t)
=
\ind\bracks{
\Delta_{\mathrm{val}}^{(w)}<\epsilon_{\Delta}
\ \land\
\mathrm{cov}_t>\eta_{\mathrm{cov}}
\ \land\
\mathrm{pass}_t>\eta_{\mathrm{pass}}
}.
$$

*Variables.* $\mathrm{advance}(t)$ is the curriculum-advancement predicate. $\Delta_{\mathrm{val}}^{(w)}$ is trailing-window validation improvement, $\mathrm{cov}_t$ is accepted-method coverage in the current phase, and $\mathrm{pass}_t$ is the Stage-3 to Stage-4 advancement rate.

## 12. Staged Evaluation and Compute Control

### 12.1 Evaluation stages

**Stage 0: patch integrity.** Patch applies uniquely, mutable boundaries are respected, AST parses, formatter and linter pass.

**Stage 1: safety and determinism smoke.** Fixed smoke task, fixed-seed replay, no forbidden access, checkpoint integrity, open-handle integrity.

**Stage 2: touched-family proxies.** Run only proxy tasks relevant to the touched scopes. Advance if

$$
\mathrm{LCB}_{\mathrm{proxy}}
=
\Delta_{\mathrm{proxy}} - 1.0 \cdot \mathrm{SE}_{\mathrm{proxy}}
>
-\epsilon_{\mathrm{proxy}}.
$$

*Variables.* $\mathrm{LCB}_{\mathrm{proxy}}$ is the lower-confidence bound on proxy improvement. $\Delta_{\mathrm{proxy}}$ and $\mathrm{SE}_{\mathrm{proxy}}$ are the proxy-set mean improvement and its standard error.

**Stage 3: objective-local training subset.** If the active objective is a single-task score, evaluate that task plus the two nearest same-family tasks. If the objective is a family average, evaluate four representative tasks from that family. If the objective is global, evaluate one task from each family. Advance if

$$
\mathrm{LCB}_{\mathrm{part}}
=
\Delta_{\mathrm{part}} - 1.0 \cdot \mathrm{SE}_{\mathrm{part}}
>
-\epsilon_{\mathrm{part}}.
$$

*Variables.* $\mathrm{LCB}_{\mathrm{part}}$ is the lower-confidence bound on the objective-local training subset. $\Delta_{\mathrm{part}}$ and $\mathrm{SE}_{\mathrm{part}}$ are the mean child-minus-parent improvement and its standard error on the subset.

**Stage 4: full training suite.** Evaluate all of $X_{\mathrm{train}}$ under the full seed set. Archive insertion and scope credit are computed only here. Within Stage 4, early rejection is allowed by minibatch:

$$
\bar{d}_B + 1.96\, \mathrm{se}_B < -\delta_{\mathrm{rej}}.
$$

*Variables.* $\bar{d}_B$ and $\mathrm{se}_B$ are the mean child-minus-parent score delta and its standard error on the current full-train minibatch $B$. $\delta_{\mathrm{rej}}$ is the early-rejection margin.

**Stage 5: periodic validation.** Evaluate current leaders on $X_{\mathrm{val}}$ without exposing those traces to the mutator.

### 12.2 Compute budget accounting

If $N_{\mathrm{mut}}$ mutations are attempted and stage pass rates are $p_1$, $p_2$, and $p_3$, then expected full-suite task-runs are

$$
N_{\mathrm{full}} = N_{\mathrm{mut}}\, p_1 p_2 p_3\, |X_{\mathrm{train}}|\, R.
$$

*Variables.* $N_{\mathrm{full}}$ is the expected number of full-suite task-seed runs. $p_1$, $p_2$, and $p_3$ are pass rates through Stages 1, 2, and 3, respectively.

Recommended pass-rate caps are $p_1\le 0.35$, $p_2\le 0.15$, and $p_3\le 0.05$. If they are exceeded, the evaluator should tighten thresholds before search proceeds.

## 13. Deterministic Implementation Notes

1. The canonical stored agent is never run directly. Clone-on-run is mandatory.
2. Horizontal workers share only the append-only board, with locks and per-worker read cursors.
3. Message-board state and open-handle tables must survive compaction and resume.
4. Short-term memory is append-only except summary replacement with preserved backlinks.
5. Long-term memory resets per evaluation unit unless transfer is explicitly scored.
6. Category-first tool discovery is mandatory; loading the entire tool tree into context is forbidden.
7. Sandbox reuse must be content-addressed by the hash in the tooling section.
8. Exact symbol and path matches dominate embedding similarity in retrieval and deduplication.
9. Merge order for worker outputs must be deterministic.
10. Validation and test traces may never appear in mutation prompts.

## 14. Minimal Reconstruction Sequence

**Algorithm 4. Reconstruction from scratch**

1. Implement the fixed shell: agent pool, tool registry, sandbox manager, short-term graph, long-term graph, open-handle table, benchmark adapters, verifiers, and safety guards.
2. Implement one baseline self-programming runtime with handwritten policies for topology, memory, tooling, and control.
3. Implement the mandatory schemas in Section~2.
4. Build proxy suites for decomposition, retrieval, deduplication, build-versus-reuse, category ranking, async dispatch, checkpoint integrity, and resume fidelity.
5. Implement the predictor family and online update loop.
6. Implement the archive, scope scheduler, credit updates, and crossover.
7. Implement the staged evaluator.
8. Run local, then pairwise, then joint evolution under the curriculum.
9. Choose leaders on validation only.
10. Evaluate final selected runtimes once on held-out tasks with frozen shell, frozen model mappings, frozen sandboxes, and $R=5$ seeds.

## 15. Recommended Defaults

| Parameter group | Default values |
|---|---|
| Core evaluation | $R_{\mathrm{proxy}}=1$, $R_{\mathrm{full}}=3$, $R_{\mathrm{val}}=5$, $R_{\mathrm{test}}=7$; $\beta_{\mathrm{sel}}=2.5$; $\delta_f=0.002$; $\theta_{\mathrm{create}}=0.58$; $K_{\max}=3$ |
| Robustness | $\eta_{\sigma}=0.35$; $\alpha=\tfrac{1}{3}$; use $\rho_x$ for archive search and $\chi_x$ for validation tie-breaks |
| Memory | $(B_{\mathrm{hi}},B_{\mathrm{lo}})=(0.75,0.55)$; $(\theta_e,\theta_{\ell})=(0.92,0.60)$ |
| Tool promotion | $(\eta_p,\eta_r)=(0.80,\ 3\ \text{distinct tasks})$; $k_c\in\set{3,4,5}$; $t_{\mathrm{slice}}=60$ s |
| Mutation budget | 1--4 patch blocks per mutation; $(N_{\mathrm{local}},N_{\mathrm{pair}},N_{\mathrm{joint}})=(1200,600,300)$ |
| Curriculum thresholds | $\epsilon_{\Delta}=0.002$; $\eta_{\mathrm{cov}}=0.60$; $\eta_{\mathrm{pass}}=0.05$ |
| Predictors | retrain after 50 fully evaluated children or 10 accepted elites; calibrate on the most recent 200 labels per task family; bootstrap ensemble size $B=5$ |
| Compute caps | recommended pass-rate caps $(p_1,p_2,p_3)\le (0.35,0.15,0.05)$ |

If strong determinism cannot be guaranteed by the model endpoint, set generation temperature to $0$ and keep repeated seeds enabled exactly as above.

## 16. Failure Modes and Non-Negotiable Invariants

The following mismatches materially change results and therefore define the non-negotiable invariants of the framework.

1. Mutating the fixed shell is disallowed.
2. Executing canonical stored agents directly is disallowed.
3. Long-term memory carryover across nominally independent tasks is disallowed unless transfer is explicitly scored.
4. Promoting synthesized tools without deterministic tests, explicit safety checks, and distinct-task reuse evidence is disallowed.
5. Destroying raw-output reachability during compaction is disallowed.
6. Losing message-board state or open-handle state during summarization or resume is disallowed.
7. Full-suite archive scores must come only from full training evaluation.
8. Validation and held-out tasks must remain invisible to the mutator.
9. Environment reuse based on mutable container state rather than content hashes is disallowed.
10. Category-first discovery may not be bypassed by loading the entire tool registry into prompt context.
11. Using a stop rule that terminates without a verified terminal artifact when the benchmark requires verification is disallowed.
12. Ignoring objective-conditioned scope credit or pairwise interaction credit materially weakens joint co-evolution and changes the method.

## 17. Closing Statement

Agintor is a bounded evolutionary program-search method over the parts of a self-programming agent runtime that actually determine downstream behavior: which agents are created, what evidence is remembered, which tools are built or reused, which models and verifiers are invoked, and when the system stops. The method requires topology, memory, tooling, and control to be co-evolved under verifier-based selection inside an immutable shell. Anything looser is a different method.