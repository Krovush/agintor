# LangGraph and Oracle Refactor Plan pass 1

## 0. Final Intent Extracted From The May 10 Prompt Chain

The user is not asking for a trading-only oracle, a game-engine oracle, a hidden-answer benchmark system, or a generic LangChain rewrite.

The target is:

1. Refactor Agintor so built runtimes are spec-backed LangGraph/LangChain multi-agent systems.
2. Keep Agintor as the factory, evaluator, search engine, evidence authority, and promotion authority around those runtimes.
3. Build the missing heart: an adaptive, LLM-led OracleCompiler that turns each original user goal into a frozen validation package.
4. Use open-source frameworks and benchmark/eval patterns wherever possible instead of rebuilding everything from scratch.
5. Treat TradingAgents as the first/default concrete runtime seed and validator family example, not as the definition of the oracle.

The most important correction from the transcript is that the oracle must be general. It may infer that a trading runtime should be judged by post-close profitability, alpha, data-cutoff integrity, order validity, and risk constraints, but that must be compiler output for one goal. The root system must not hardcode finance as the oracle shape.

The correct stable split:

```text
Agintor factory
  -> GoalSpec
  -> RuntimeSpec
  -> OracleCompiler
  -> FrozenOraclePackage
  -> LangGraph/LangChain runtime candidates
  -> sealed paired evaluation
  -> EvidenceLedger
  -> ProgressOracle
  -> search/archive/promotion update
```

## 1. Product Decision

Agintor should become a validation-backed MAS factory.

The built product should be a normal LangGraph/LangChain multi-agent runtime generated from a typed spec. The factory remains custom because LangGraph does not decide what to build, what evidence is private, which validators are trustworthy, how candidate improvements are compared, or when a mutation is allowed to update search.

The runtime refactor and oracle refactor should be implemented together in a narrow vertical slice:

1. Add a minimal `RuntimeSpec` and compile it into LangGraph/LangChain runtime code.
2. Add `OraclePackage` as the frozen validation artifact generated from the same `GoalSpec`.
3. Run one end-to-end evolution loop where a spec action mutates a runtime, the same frozen oracle package evaluates parent and child, and `ProgressOracle` decides whether the mutation can update search.

Do not finish a large LangGraph migration before the oracle exists. Do not build the oracle deeply against the old mutable Python policy runtime. The first useful system is small but complete.

## 2. Current Repo State To Build On

Agintor already has several load-bearing pieces. The plan should extend them, not pretend the repo is empty.

Important implementation boundary:

- The LangGraph v2 runtime and adaptive oracle compiler are still design targets, not landed code. The live repo has no LangGraph dependency yet and no implemented `RuntimeSpec`, `OracleCompiler`, or `ValidationPlan` module.
- The current exported runtime is policy-file v1. `RuntimeManifest` still points at mutable Python policy modules, and `load_runtime()` imports those policy classes into a `LoadedRuntime`.
- The v2 path should therefore be added as a parallel runtime kind selected by manifest/spec metadata, not by pretending the current runtime already is LangGraph-backed.
- The strongest seam to reuse is already present: prompt solves and benchmark solves compile into `ExecutionPlan`/`PlanNode`, enter through `RuntimeHost` and the bundled runtime entrypoint, and execute through runtime-kernel operation code.
- LangGraph state should be embedded inside the existing `CheckpointEnvelope`; do not introduce a second checkpoint ABI or storage-version axis.

### Existing factory and planning

- `agintor/contracts/factory.py` defines `GoalSpec`, `BenchmarkPlan`, `RuntimePlan`, and `BuildSummary`.
- `agintor/factory/planning.py` builds goal-conditioned suites, a `BenchmarkPlan`, and a `VerifierBundle`.
- `BenchmarkPlan` is currently mostly task IDs plus a verifier bundle ID. It should remain the task-selection projection, not the full validation contract.
- `VerifierBundle` currently stores loose `VerifierSpec` records. It needs to become either a projection from, or companion to, typed validator specs in an `OraclePackage`.

### Existing runtime boundary

- `ExecutionPlan` is already the canonical solve-time plan for prompt and benchmark execution.
- `RuntimeHost`, exported runtime entrypoint, Docker/local backend handling, path rewriting, checkpoint/run-store semantics, and private benchmark projection already exist.
- The LangGraph refactor doc already picks the correct direction: add a parallel `langgraph_spec_v2` runtime path, keep one `RUNTIME_CONTRACT_VERSION`, preserve the host/runtime boundary, and compile `ExecutionPlan` into a graph-backed executor.

### Existing evaluation and progress authority

- `agintor/contracts/evidence.py` already defines `DomainEvidenceContract`, `EvidenceRecord`, `OutcomeAxisScore`, `PairedComparison`, and promotion-related evidence objects.
- `agintor/evaluation/evaluator.py` already writes evidence, paired comparison, and promotion ledgers during Stage 4 evaluation.
- `agintor/evaluation/progress_oracle.py` already decides `capability`, `efficiency`, `subskill`, `reject`, `abstain`, `quarantine`, and `no_progress`.
- That current `ProgressOracle` should stay the decision gate. It should not become the LLM compiler.

### Existing search

- `agintor/search/engine.py` runs staged mutation/evaluation, records history, updates the archive, scheduler, predictors, and `signal_sufficiency.json`.
- `agintor/search/mutators.py` currently mutates Python policy files through search/replace or model-generated patches.
- For v2 runtimes, this becomes `SpecActionMutator`: mutate typed `RuntimeSpec` actions instead of patching Python policy files.

## 3. External Resources To Use

Use these as adapters and design references. Do not outsource Agintor's product spine to any one of them.

| Resource | Use it for | Do not use it for |
|---|---|---|
| LangGraph | Runtime graph execution, state, checkpointable workflows, resumable multi-agent orchestration. Official docs describe durable execution, persistence, threads, checkpoints, replay, and fault tolerance. Sources: https://docs.langchain.com/oss/python/langgraph/overview, https://docs.langchain.com/oss/python/langgraph/persistence | Oracle authority, promotion rules, evidence fusion, or optimizer updates. |
| LangChain | Model/tool bindings, `create_agent` where an agent loop is useful, middleware for dynamic tool/model/prompt behavior. Source: https://docs.langchain.com/oss/python/langchain/agents | Durable evolutionary representation. Do not store live LangChain object instances as the genome. |
| LangSmith | Tracing, experiment comparison, datasets, human review, evaluator management, redaction controls. Sources: https://docs.langchain.com/langsmith/evaluation, https://docs.langchain.com/langsmith/evaluators, https://docs.langchain.com/langsmith/mask-inputs-outputs | Sealed private oracle storage or final promotion authority. |
| TradingAgents | First/default runtime seed for a finance MAS: LangGraph-based trading agents, structured outputs, checkpoint resume, persistent decision logs. Source: https://github.com/TauricResearch/TradingAgents | The general oracle architecture. Trading is one validator family and one runtime profile. |
| METR Task Standard | Best shape for `OraclePackage`: task family, environment, public instructions, scorer, hidden info, aux VM pattern, task QA. Source: https://github.com/METR/task-standard | Agintor's whole runtime or promotion system. It is a task format, not a MAS factory. |
| Inspect AI | Optional runner for validators, scorers, solvers, sandboxes, multiple scorers, task packaging, Docker evals. Sources: https://inspect.aisi.org.uk/tasks.html, https://inspect.aisi.org.uk/scorers.html, https://inspect.aisi.org.uk/sandboxing.html | Oracle trust semantics. Inspect can execute evals; Agintor decides authority. |
| SWE-bench and SWE-ReX | Repo-patch validator family, Docker-style issue repair tasks, sandbox execution patterns. Source: https://github.com/swe-bench | General validation compiler. Coding-only and benchmark-specific. |
| tau-bench / tau2 / tau3 | Stateful tool-agent-user service simulation, policy compliance, API-state validators. Source: https://arxiv.org/abs/2406.12045 | General oracle core. It is a domain pattern. |
| OpenAI Evals | Private eval patterns, grader templates, custom eval execution, completion-function style adapters. Source: https://github.com/openai/evals | Strong promotion authority by itself. Model graders require calibration and authority caps. |
| AgentEvals / OpenEvals | Trajectory, graph trajectory, strict/LLM judge evaluators for LangGraph/LangChain agents. Source: https://github.com/langchain-ai/agentevals | Final truth. Useful for trace validators only. |
| DSPy / Promptim / Cognify / AFlow / ADAS / EvoAgentX | Candidate optimization ideas, prompt/node optimization, workflow mutation/search operators. | Trust boundary. They optimize whatever metric they are given, so they must sit downstream of frozen oracle packages. |
| AutoGen / Microsoft Agent Framework / CAMEL | Alternative MAS orchestration patterns and agent-society ideas. | Default substrate for this pass. LangGraph is already aligned with the repo direction and TradingAgents target. |

The best combined design to adapt is:

```text
METR-like OraclePackage format
  + Inspect/OpenAI/AgentEvals/SWE/tau adapters for validator execution
  + LangGraph/LangChain as runtime substrate
  + Agintor-owned EvidenceLedger and ProgressOracle
```

## 4. Target Architecture

### 4.1 Built runtime path

The built runtime is a generated LangGraph/LangChain app, not a pile of mutator-visible policy files.

```text
GoalSpec
  -> RuntimeSpec
  -> RuntimeSpec compiler
  -> exported LangGraph/LangChain runtime app
  -> Agintor runtime protocol entrypoint
```

The durable representation is `runtime_spec.json`, not live Python objects and not raw generated code. The compiler emits normal Python app files, but search mutates the spec through typed actions.

### 4.2 Oracle path

The oracle is not one validator and not one score. The adaptive part is the compiler. The judgment part is frozen.

```text
GoalSpec + RuntimeSpec + prior ledgers
  -> OracleCompiler
  -> OraclePackageDraft
  -> OraclePackageQA
  -> FrozenOraclePackage
  -> public task projection + sealed evaluator authority
```

### 4.3 Evaluation path

```text
parent RuntimeSpec + child RuntimeSpec
  -> run both against same public task views
  -> sealed validators evaluate outputs/traces/artifacts/states
  -> claim-level evidence ledger
  -> paired comparison
  -> ProgressOracle decision
  -> search update only if decision allows it
```

### 4.4 Core invariant

Candidate runtimes may see public task views, public fixtures, allowed tools, their own trace, and their own result. They must not see sealed fixtures, private expected states, hidden tests, promotion thresholds, validator internals, private rubrics, holdout outcomes, or raw oracle package secrets.

## 5. Object Model

### 5.1 RuntimeSpec

New module: `agintor/contracts/runtime_spec.py`

```python
class RuntimeSpec(BaseModel):
    schema_version: Literal["agintor.runtime_spec.v2"]
    runtime_id: str
    runtime_kind: Literal["langgraph_spec_v2", "tradingagents_langgraph_v1"]
    name: str
    description: str
    agents: list[AgentSpec]
    graph: GraphSpec
    tools: list[ToolSpec]
    models: list[ModelPolicy]
    memory: MemoryPolicy
    execution: ExecutionPolicy
    tracing: TracingPolicy
    mutation_history: list[MutationActionRef]
    parent_spec_digest: str | None = None
    metadata: dict[str, Any] = {}
```

Rules:

- Canonical JSON normalization produces `spec_digest`.
- All graph node IDs, tool IDs, model policy IDs, and agent IDs are stable.
- Runtime identity includes the normalized spec and exported runtime-owned code.
- Private oracle fields are not allowed in `RuntimeSpec`.
- The exported runtime includes `runtime_spec.json` and enough generated app code to run without importing factory-only modules.

### 5.2 SpecAction

New module: `agintor/contracts/spec_actions.py`

```python
class SpecAction(BaseModel):
    action_id: str
    action_type: Literal[
        "add_agent",
        "remove_agent",
        "update_agent",
        "set_edge",
        "remove_edge",
        "set_tool_policy",
        "set_model_policy",
        "set_memory_policy",
        "set_budget_policy",
        "set_routing_policy",
        "set_prompt",
    ]
    target_ids: list[str]
    scope: list[Literal["top", "mem", "tool", "ctl"]]
    rationale: str
    expected_effect: str
    patch: dict[str, Any]
```

Rules:

- Actions validate against the parent spec before application.
- Actions cannot add private validator tools to runtime-visible tool lists.
- Actions write `mutation_ledger.jsonl`.
- Search archives store `mutation_action_ids`, `parent_spec_digest`, and `child_spec_digest`.

### 5.3 OraclePackage

New module: `agintor/contracts/oracle.py`

```python
class OraclePackage(BaseModel):
    package_id: str
    oracle_family_id: str
    package_hash: str
    goal_id: str
    runtime_spec_digest: str
    validation_intent: ValidationIntent
    claim_graph: ClaimGraph
    proof_obligations: list[ProofObligation]
    validator_specs: list[ValidatorSpec]
    task_sets: list[OracleTaskSet]
    fixture_bundle_refs: list[FixtureBundleRef]
    evidence_contract: DomainEvidenceContract
    scoring_projection: ScoringProjection
    authority_policy: AuthorityPolicy
    leakage_policy: LeakagePolicy
    abstention_policy: AbstentionPolicy
    qa_report_ref: str
    public_view_hash: str
    sealed_view_hash: str
    frozen: bool = True
```

Rules:

- The public package projection is runtime-visible.
- The sealed package projection is evaluator-only.
- Package hash closes over both projections plus all referenced sealed artifacts.
- Package freeze happens before candidate evaluation.
- If the package changes, parent and child comparisons are blocked unless both are re-evaluated under the same package hash or a bridge evaluation explicitly marks comparability.

### 5.4 ValidationIntent

This is the compiler's compact interpretation of what validation means for the user goal.

```python
class ValidationIntent(BaseModel):
    task_classes: list[str]
    required_capabilities: list[str]
    user_weights: dict[str, float]
    hard_failures: list[str]
    acceptable_tradeoffs: list[str]
    authority_floor: str
    unverifiable_residual_policy: Literal["abstain", "human_audit", "diagnostic_only"]
```

### 5.5 ClaimGraph

```python
class ClaimSpec(BaseModel):
    claim_id: str
    text: str
    claim_type: Literal[
        "outcome",
        "state",
        "process",
        "safety",
        "factual",
        "semantic",
        "architecture",
        "cost",
    ]
    criticality: Literal["hard", "major", "minor", "diagnostic"]
    weight: float
    minimum_authority: str
    dependencies: list[str]
    unverifiable_reason: str = ""
```

Rules:

- Critical claims must have a validator or an explicit abstention path.
- A runtime that emits opaque output without claim/evidence references may run, but it is not promotable.
- Claim-level authority does not generalize to unrelated claims.

### 5.6 ValidatorSpec

```python
class ValidatorSpec(BaseModel):
    validator_id: str
    family_id: str
    claim_ids: list[str]
    inputs: dict[str, Any]
    outputs_schema: dict[str, Any]
    authority_ceiling: str
    visibility: Literal["public", "private", "sealed"]
    independence_group: str
    leakage_risk: str
    health_tests: list[str]
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"]
```

Validators emit structured observations. They do not decide promotion.

## 6. OracleCompiler Agentic System

The OracleCompiler should be lightweight and LLM-led. Deterministic code supports the workflow; it does not predefine every domain.

### 6.1 Compiler graph

Build the compiler itself as a LangGraph workflow:

```text
START
  -> goal_interpreter
  -> runtime_context_reader
  -> task_class_inferencer
  -> claim_decomposer
  -> validator_family_router
  -> benchmark_designer
  -> fixture_and_evaluator_designer
  -> authority_and_abstention_designer
  -> package_writer
  -> critic
  -> deterministic_qa_runner
  -> freeze_or_abstain
END
```

### 6.2 Compiler subagents

The coordinator can launch specialist subagents:

- Goal analyst: extracts success criteria, hard failures, user weights, and ambiguity.
- Domain analyst: finds likely task class and validator families.
- Benchmark designer: proposes task sets and held-out variants.
- Validator author: writes or configures validators.
- Fixture author: creates public and sealed fixtures.
- Leakage critic: searches for ways the candidate could see private authority.
- Health critic: creates positive controls, negative controls, vacuity checks, and tamper checks.
- Package finalizer: emits typed JSON artifacts.

The coordinator remains the only writer of the final package. Specialist outputs are proposals.

### 6.3 Deterministic helper scripts

Helpers should be boring and strict:

- Schema validation.
- Canonical JSON normalization.
- Package hashing and lockfile generation.
- Public/sealed projection checks.
- Private metadata leakage scan.
- Validator health runner.
- Fixture determinism check.
- Benchmark smoke runner.
- Ledger append and replay check.
- Package diff.

Helpers should not call LLMs, silently rewrite package semantics, or mutate a frozen package.

## 7. Validator Family Registry

New module: `agintor/oracle/validator_registry.py`

The compiler should compose from a registry instead of inventing every validator from scratch.

Initial families:

1. `exact_private_answer`
   - Uses private expected values.
   - Keep as trust-boundary canary only.

2. `schema_artifact`
   - Validates JSON, files, reports, structured outputs, and artifact contracts.

3. `repo_patch`
   - Uses SWE-bench-style patch application, public tests, hidden tests, mutation tests, no test tampering, diff sanity.

4. `stateful_service`
   - Uses tau-bench-style simulated users, API policies, fixture database state, final state diffs, duplicate side-effect checks.

5. `trace_state`
   - Uses LangGraph/AgentEvals trajectory checks, required nodes/tool calls, forbidden nodes/tool calls, budget and side-effect receipts.

6. `factual_grounded`
   - Uses retrieval, citation support, source freshness, contradiction checks, and calibrated weak judges.

7. `pairwise_preference`
   - Uses human or model preference evidence with authority caps and calibration.

8. `trading_outcome`
   - Uses TradingAgents-style decision/order/fill/outcome ledgers, post-close outcome, data cutoff, portfolio reconciliation, cost/slippage, risk constraints.
   - This is a default family for finance goals, not the oracle core.

9. `human_audit`
   - Stores signed human review references as bounded authority evidence.

10. `inspect_runner`
   - Adapter to Inspect tasks, scorers, solvers, and Docker sandboxes.

11. `openai_eval_runner`
   - Adapter to OpenAI Evals/API eval workflows where useful.

## 8. How TradingAgents Fits

TradingAgents should become the first serious default runtime seed if the user's factory goal is finance/trading. It is already a LangGraph MAS pattern with analysts, researchers, trader, risk, portfolio manager, structured outputs, checkpoint resume, and persistent decision logs.

Agintor should not fork it into an unrecognizable custom kernel first. Start with an adapter.

New package:

```text
agintor/integrations/tradingagents/
  spec.py
  adapter.py
  compiler.py
  action_mapper.py
  data_snapshots.py
  ledgers.py
  validators.py
  outcome_oracle_family.py
```

`TradingAgentsRuntimeSpec` should be a profile of `RuntimeSpec`, not a separate product line:

```python
class TradingAgentsRuntimeSpec(RuntimeSpec):
    runtime_kind: Literal["tradingagents_langgraph_v1"]
    selected_analysts: list[str]
    deep_think_model: str
    quick_think_model: str
    debate_rounds: int
    risk_discussion_rounds: int
    data_vendor_policy: dict[str, Any]
    memory_policy: dict[str, Any]
    action_mapping_policy_id: str
    risk_policy_id: str
```

The trading validator family should infer these claims when relevant:

- Decisions were made with data available only before the decision cutoff.
- Final recommendations map to valid bounded order intents.
- Fills reconcile with orders.
- Portfolio state reconciles with fills, cash, costs, and positions.
- EOD scoring uses frozen price snapshots.
- Outcome metrics are computed consistently.
- Risk policy is obeyed.
- Runtime identity and spec digest match the evaluated candidate.

For a trading goal, the compiler may choose post-close net PnL, alpha, drawdown, cost, and risk-adjusted metrics. For a non-trading goal, it must choose something else.

## 9. LangGraph / LangChain Runtime Implementation

### 9.1 Runtime compiler

New package:

```text
agintor/runtime/langgraph/
  compiler.py
  state.py
  operation_service.py
  adapters.py
  checkpointing.py
```

The compiler turns `RuntimeSpec` into a normal LangGraph app:

1. Validate spec.
2. Build typed runtime state.
3. Convert agents into LangGraph nodes.
4. Convert tools into LangChain tool bindings where useful.
5. Convert deterministic routing/merge/verify steps into plain graph nodes.
6. Compile `StateGraph`.
7. Expose the same Agintor runtime protocol entrypoint.

### 9.2 Use LangChain selectively

Use LangChain for:

- Model provider abstraction where it reduces glue.
- Tool definitions and middleware.
- `create_agent` for actual agent-loop nodes.
- Structured output helpers.
- Dynamic model/tool filtering.

Do not use LangChain for:

- Durable evolutionary genome representation.
- Private validators.
- Private fixture storage.
- Promotion decisions.
- Evidence ledger authority.
- Search credit assignment.

### 9.3 RuntimeOperationService

Extract current runtime operation behavior into a shared service. LangGraph nodes call it instead of reimplementing current behavior.

Responsibilities:

- Tool execution.
- Provider/model calls.
- Repo/file/service operations.
- Side-effect receipts.
- Budget accounting.
- Trace events.
- Runtime-visible task projection.
- Path rewriting compatibility.
- Docker/local backend behavior.

This is the main anti-rewrite rule. LangGraph changes graph execution; it should not break the runtime-host contract.

## 10. Evaluation And Ledger Changes

### 10.1 Add OraclePackage loading

`RuntimeEvaluator` should load a frozen `OraclePackage` for an evaluation. It passes public task views to the runtime and keeps sealed validator authority in the evaluator.

### 10.2 Expand evidence from scalar to claim records

Keep current `EvidenceRecord`, but add companion claim-level records:

```python
class ValidatorResult(BaseModel):
    validator_id: str
    claim_ids: list[str]
    status: Literal["pass", "fail", "error", "abstain"]
    authority_used: str
    health_status: dict[str, Any]
    observations: dict[str, Any]
    evidence_digest: str

class ClaimResult(BaseModel):
    claim_id: str
    satisfied: bool | None
    posterior_lower: float | None = None
    posterior_upper: float | None = None
    authority_mass: dict[str, float]
    coverage: float
    residual_unverified: str = ""
```

The scalar score may remain as a projection, but the ledger is the authority.

### 10.3 Pairwise comparison rule

Parent and child can be compared only when:

- Same frozen `oracle_package_hash`.
- Same runtime contract version.
- Same task partition.
- Same budget class or explicit cost-adjusted comparison.
- Same evaluator policy.
- Same public/sealed package digests.

If the OracleCompiler creates a new package after seeing weak evidence, the new package is diagnostic until both parent and child are rerun under it.

## 11. ProgressOracle Changes

Keep `ProgressOracle` as the decision layer.

Do not put LLM calls or package generation in `progress_oracle.py`.

Required changes:

1. Accept claim-level evidence summaries in addition to current axis deltas.
2. Treat `DomainEvidenceContract` as a projection of `OraclePackage.evidence_contract`.
3. Refuse promotion when critical claims are unverified.
4. Keep `abstain` for insufficient authority.
5. Keep `quarantine` for leakage, tampering, invalid package, or corrupted ledger.
6. Report `decision_type`, reason codes, improved axes, regressed axes, `oracle_package_hash`, and evidence digest.
7. Preserve existing capability/efficiency/subskill routing so search/archive behavior remains compatible.

## 12. Search And Mutation Changes

### 12.1 Runtime mutation

Replace policy-file patch mutation for v2 runtimes with `SpecActionMutator`.

```text
parent RuntimeSpec
  -> selected scope/objective
  -> propose SpecAction list
  -> apply actions
  -> validate spec
  -> compile runtime
  -> smoke run
  -> evaluate under frozen OraclePackage
```

### 12.2 Oracle evolution

Runtime evolution and oracle evolution are separate loops.

Runtime evolution:

```text
same frozen oracle package
  -> compare parent/child
  -> promote/reject/abstain/quarantine
```

Oracle evolution:

```text
signal_sufficiency gap or new user goal amendment
  -> OracleCompiler drafts new package
  -> QA gate
  -> freeze new package revision
  -> bridge/re-evaluate if comparisons are needed
```

The search loop cannot make the current child look better by editing the oracle after the child fails.

### 12.3 Archive and scheduler

Archive records should store:

- `runtime_id`
- `runtime_hash`
- `spec_digest`
- `parent_spec_digest`
- `oracle_package_hash`
- `contract_id`
- `evidence_digest`
- `promotion_decision_type`
- `mutation_action_ids`
- `authority_summary`
- `risk_summary`

Scheduler/predictor updates should require a verified training signal. A scalar reward without a ledger digest must not update search.

## 13. Export And UX

### 13.1 Exported runtime contents

Export includes:

```text
runtime_spec.json
runtime_manifest.json
generated LangGraph/LangChain app files
runtime protocol entrypoint
deployment_contract.json
public_validation_summary.json
public_evidence_summary.json
```

Export must not include:

```text
sealed oracle package
private fixtures
private expected values
hidden tests
promotion thresholds
private rubrics
oracle compiler traces that reveal private authority
```

### 13.2 CLI surfaces

Add:

```bash
agintor inspect-runtime <dir>
agintor diff-runtime <parent> <child>
agintor inspect-oracle <package_or_build_dir> --public
agintor diff-oracle <old> <new>
agintor oracle-qa <package_dir>
```

Do not build a visual graph UI in pass 1. Mermaid export is fine if cheap.

## 14. File-Level Implementation Plan

### Phase 1: Read-only contracts and helpers

Add:

```text
agintor/contracts/runtime_spec.py
agintor/contracts/spec_actions.py
agintor/contracts/oracle.py
agintor/oracle/package_io.py
agintor/oracle/projections.py
agintor/oracle/qa.py
tests/test_runtime_spec.py
tests/test_oracle_package.py
```

Exit gates:

- Schemas validate.
- Spec digest is stable.
- Package digest is stable.
- Public projection strips private fields.
- Frozen package cannot be mutated.
- `python -m compileall -q agintor` passes.

### Phase 2: Current behavior as an OraclePackage

Wrap the current demo/tool-frontier evidence contract into an `OraclePackage` without changing runtime behavior.

Touch:

```text
agintor/factory/planning.py
agintor/evaluation/benchmarks.py
agintor/contracts/evidence.py
agintor/evaluation/evaluator.py
```

Exit gates:

- Existing WS4 tests pass.
- Existing evidence and promotion ledgers include `oracle_package_hash`.
- Current `ProgressOracle` behavior is unchanged for existing tests.

### Phase 3: Evaluator carries public/sealed package views

Evaluator must project runtime-visible tasks from the public package view and use sealed package refs for host-side validation.

Touch:

```text
agintor/evaluation/evaluator.py
agintor/runtime/host/*
agintor/contracts/benchmarks.py
tests/test_oracle_public_projection.py
tests/test_oracle_sealed_eval.py
```

Exit gates:

- Candidate runtime cannot see sealed fields.
- Private expected values and private metadata do not appear in runtime-visible payloads.
- Evidence ledgers cite package, task, validator, and digest identity.

### Phase 4: RuntimeSpec v2 and spec actions

Implement the minimal spec-backed runtime path.

Touch:

```text
agintor/contracts/runtime.py
agintor/runtime/loader.py
agintor/runtime/sdk/bundle.py
agintor/runtime/langgraph/*
agintor/search/spec_mutator.py
agintor/factory/export.py
templates/baseline_runtime_v2/
```

Exit gates:

- `init-runtime` can create a v2 runtime.
- `build-runtime` can export a self-contained v2 runtime.
- v2 deterministic prompt solve works with zero model calls.
- v2 benchmark solve works through `RuntimeHost`.
- Runtime identity changes when `runtime_spec.json` changes.

### Phase 5: SpecActionMutator in search

Wire v2 mutation into staged evolution.

Touch:

```text
agintor/search/engine.py
agintor/search/archive.py
agintor/search/spec_mutator.py
agintor/contracts/search.py
agintor/factory/export.py
```

Exit gates:

- Stage 0 applies spec actions instead of Python patches for v2.
- Mutation ledger is written.
- Stage 4 promotion ledger links decision to action IDs.
- Archive can select a v2 parent and export a v2 leader.

### Phase 6: LLM OracleCompiler behind a flag

Add the adaptive compiler but keep it opt-in until QA is strong.

Add:

```text
agintor/oracle/compiler.py
agintor/oracle/compiler_graph.py
agintor/oracle/subagents.py
agintor/oracle/validator_registry.py
agintor/oracle/families/
```

CLI/profile flag:

```bash
agintor build-runtime "<goal>" --oracle-compiler adaptive
```

Exit gates:

- Compiler outputs typed JSON/Pydantic objects, not prose.
- QA rejects packages with missing critical validators, leakage, vacuity, or invalid schemas.
- Compiler can emit a package equivalent to current tool-frontier behavior.
- Compiler can emit a repo-patch package using local tests/fixtures.
- Compiler can emit a TradingAgents package from a finance goal without hardcoded root oracle logic.

### Phase 7: Validator family registry

Implement initial families:

```text
agintor/oracle/families/schema_artifact.py
agintor/oracle/families/repo_patch.py
agintor/oracle/families/stateful_service.py
agintor/oracle/families/trace_state.py
agintor/oracle/families/pairwise_preference.py
agintor/oracle/families/trading_outcome.py
```

Exit gates:

- Each family declares applicability, authority ceiling, inputs, outputs, health tests, leakage risks, and failure action.
- Each family can run at least one positive control and one negative control.
- Weak LLM/human validators cannot produce high-authority promotion alone.

### Phase 8: TradingAgents default seed

Only after the generic compiler path exists, add TradingAgents as the first serious default runtime seed.

Touch:

```text
agintor/integrations/tradingagents/*
agintor/oracle/families/trading_outcome.py
tests/test_tradingagents_adapter.py
tests/test_trading_oracle_package.py
```

Exit gates:

- Adapter can load/compile a TradingAgents-shaped spec.
- Compiler can infer trading outcome validators from a trading goal.
- Evidence is software-evaluation evidence: data cutoff, order validity, fill reconciliation, portfolio state, costs, risk policy, runtime identity, and post-close outcome.
- No trading-specific assumptions leak into non-trading oracle compilation.

## 15. Testing Strategy

Run focused tests, not endless broad tests first.

Minimum validation slices:

```bash
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_oracle_package.py tests/test_oracle_public_projection.py
.\.venv\Scripts\python -m pytest tests/test_runtime_spec.py tests/test_spec_actions.py
.\.venv\Scripts\python -m pytest tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py
.\.venv\Scripts\python -m pytest tests/test_runtime_host.py tests/test_runtime_execution.py
git diff --check
```

Add integration tests only after the vertical slice is green:

```bash
.\.venv\Scripts\python -m pytest -m integration --basetemp .tmp_pytest_integration
```

## 16. Completion Definition For Pass 1 Implementation

The first implementation pass is done only when all of these are true:

1. New runtimes can be represented as `RuntimeSpec`.
2. New runtimes can compile into a runnable LangGraph/LangChain app behind the existing runtime protocol.
3. Runtime mutation happens through typed actions and writes a mutation ledger.
4. A frozen `OraclePackage` is created from `GoalSpec`.
5. The package has public and sealed projections.
6. Evaluator gives candidates only the public projection.
7. Evidence records cite package hash, contract ID, validator IDs, and runtime spec digest.
8. `ProgressOracle` remains the promotion gate.
9. Search/archive updates require an evidence digest and decision type.
10. TradingAgents can be registered as a default runtime/validator family without hardcoding finance into the generic compiler.

## 17. What Not To Build In Pass 1

- No visual graph UI.
- No full framework marketplace.
- No broad replacement of `RuntimeHost`.
- No live mutation of LangChain object instances.
- No sealed oracle data in exported runtimes.
- No LLM judge as final promotion authority.
- No trading-only oracle.
- No game-engine anything.
- No compatibility migrations for old MVP checkpoints or exported runtimes.

## 18. Immediate Next Move

Start with the artifact spine:

1. Add `RuntimeSpec`, `SpecAction`, and `OraclePackage` contracts.
2. Add package hash/projection/QA helpers.
3. Wrap the current `DomainEvidenceContract` into a no-behavior-change `OraclePackage`.
4. Thread `oracle_package_hash` through evaluation ledgers.
5. Then implement the minimal LangGraph runtime compiler.

This order gives Agintor a real validation identity before the runtime substrate moves too far, while still getting away from the custom policy-file runtime quickly.
