# Corrected LangGraph + Oracle Refactor Implementation Plan

## Decision

Do not apply either patch bundle wholesale.

Implement a third, rebased synthesis:

- Use `agintor_full_oracle_langgraph_stack/` as the primary design source because it has the fuller end-to-end spine: `OracleEvaluationRunner`, runnable validator families, evaluator/search/export/loader integration intent, v2 runtime generation, mutation ledgers, and TradingAgents as an example profile.
- Use `agintor_full_plan_patch/` only as a secondary source for its cleaner apply map, its smaller contract scaffolds where useful, the optional `langgraph.graph.StateGraph` import-with-fallback idea, and its inspectable static v2 template artifacts.
- Rebase every existing-file edit manually onto the current repo. Treat both bundles as proposals, not executable patches.

The target shape is:

```text
GoalSpec
  -> RuntimeSpec
  -> frozen OraclePackage
  -> runtime-visible public task projection
  -> langgraph_spec_v2 runtime candidate
  -> host/runtime protocol execution
  -> sealed evaluator-side validator execution
  -> EvidenceRecord / PairedComparison / PromotionDecision
  -> ProgressOracle
  -> search/archive update only when evidence is comparable
```

The custom Agintor product spine stays in Agintor. LangGraph is the runtime orchestration substrate. It is not the oracle, not the evidence authority, and not the promotion authority.

## Evidence From Bundle Review

### `agintor_full_plan_patch/`

Observed in isolated worktree:

- Its exact search/replace edits apply cleanly to the current tracked files.
- `python -m compileall -q agintor` passed after applying it.
- Its focused tests failed.

Supported failures:

- `RuntimeSpec` digest is not stable across object/dict round trips.
- `OracleCompiler().compile(...)` fails QA because the default compiler emits no task sets.
- `public_oracle_projection(...)` still exposes sealed field names such as `sealed_inputs` in the public rendered payload.

Architectural gaps:

- Validator families mostly declare `ValidatorSpec`s; they do not run real validators.
- No `OracleEvaluationRunner`.
- Search/evaluator wiring is thinner and does not close the v2 mutation/evaluation/promotion loop.
- The static template is useful, but it is under `templates/`; in this repo the runtime templates live under `agintor/templates/`.

### `agintor_full_oracle_langgraph_stack/`

Observed in isolated worktree:

- It is conceptually more complete.
- It must not be copied directly: it contains `__pycache__/` and `.pyc` files.
- Its exact search/replace diffs are stale or ambiguous against the current repo.
- After applying only the blocks that match, `agintor/runtime/loader.py` has an indentation error.
- Its focused tests still fail.

Supported failures:

- `OraclePackage` freezing round-trips task payloads with `exclude_none=True`, which drops required `BenchmarkTask.expected`.
- `RuntimeSpec` private/sealed scanning rejects legitimate strings such as runtime IDs containing `"sealed"` because it scans the whole rendered payload instead of only forbidden keys/fields.
- `RuntimeSpecCompiler.compile_to_directory()` calls `bundle_runtime_kernel(...)`; in a clean worktree the ignored `agintor/templates/baseline_runtime/runtime_profile.json` resource is missing.
- Existing-file edit anchors are stale in `factory/planning.py`, `contracts/search.py`, and ambiguous in `cli.py`.
- The generated v2 runtime path does not yet prove host/runtime protocol execution end to end.

What the other agent got right:

- `agintor_full_oracle_langgraph_stack/` is the better conceptual base.
- Patch's optional `StateGraph` path is worth lifting into Stack's runtime compiler/executor.
- Patch's static template is worth keeping as a smoke fixture and human-readable reference.

What must be corrected:

- Do not run Stack's `apply_search_replace.py` against the repo.
- Do not copy its `__pycache__/`.
- Do not accept the loader diff indentation.
- Do not accept Stack's broad private/sealed substring scanner.
- Do not keep Stack's hand-rolled sequential walker as the only "LangGraph" implementation.
- Do not place v2 templates in a repo-root `templates/` folder.

## Source Selection By Area

### RuntimeSpec

Use Stack's `agintor/contracts/runtime_spec.py` as the richer starting point, not Patch's thinner version.

Required corrections:

- Use one runtime kind spelling: `policy_modules_v1` for current runtimes and `langgraph_spec_v2` / `tradingagents_langgraph_v1` for v2 runtimes.
- Add `RuntimeManifest.runtime_kind`, `runtime_spec_path`, and `runtime_spec_digest` with defaults so existing v1 manifests load without migrations.
- Do not add another runtime contract version or storage ABI.
- Do not scan all string values for `"sealed"` or `"private"`. Use recursive key/path checks:
  - reject keys starting with `private_`, `sealed_`, `hidden_`, `oracle_private_`;
  - reject exact forbidden keys such as `private_expected`, `private_answer`, `private_answer_ref`, `hidden_tests`, `private_rubric`, `promotion_threshold`;
  - reject runtime-visible tools with `authority_boundary == "sealed_validator"`;
  - allow ordinary values that happen to contain words like `"sealed"` in names, IDs, or descriptions.
- Make canonical spec digest deterministic:
  - sort keys;
  - normalize Pydantic models to JSON-compatible values;
  - exclude `created_at` and other incidental timestamps;
  - include semantic runtime content, parent digest, and mutation action references;
  - decide explicitly whether `metadata` participates. For MVP, exclude general `metadata` from identity unless a specific metadata field is load-bearing.

Tests:

- `RuntimeSpec` validates the baseline spec.
- digest is stable across object/dict round trips and key ordering changes.
- digest changes when agent prompt, graph, tool, model, memory, or execution policy changes.
- forbidden private keys are rejected.
- legitimate strings containing `sealed` are not rejected.

### SpecAction

Use Stack's `agintor/contracts/spec_actions.py` as the starting point.

Required corrections:

- Keep typed actions: `add_agent`, `remove_agent`, `update_agent`, `add_node`, `remove_node`, `update_node`, `set_edge`, `remove_edge`, `add_tool`, `remove_tool`, `set_tool_policy`, `set_model_policy`, `set_memory_policy`, `set_budget_policy`, `set_routing_policy`, `set_prompt`.
- Validate target IDs before mutation.
- Validate graph integrity after mutation.
- Reject private/sealed material using the same key/path scanner as `RuntimeSpec`, not raw substring matching over all values.
- Write `SpecMutationLedgerEntry` rows with:
  - `action_id`;
  - parent/child spec digests;
  - parent/child runtime hashes when known;
  - `oracle_package_hash`;
  - `evidence_digest` once evaluation completes.
- Do not expose private validator tools or sealed oracle fixtures through `ToolSpec`.

Tests:

- valid action mutates spec and changes digest;
- invalid target fails before mutation;
- removing referenced agent/tool fails;
- action patch with private key fails;
- mutation ledger JSONL rows are deterministic and append-only.

### OraclePackage Contract

Use Stack's `agintor/contracts/oracle.py` as the conceptual source because it wraps existing `BenchmarkTask` instead of inventing a parallel task model.

Required corrections:

- `OracleTask` must wrap `BenchmarkTask`.
- Any compiler-created `BenchmarkTask` must include `expected` explicitly. `expected=None` is valid because the field type is `Any`, but the field must not be omitted during round trips.
- Do not serialize `BenchmarkTask` with `exclude_none=True` when it may have `expected=None`; that caused the Stack failure.
- `OraclePackage` must include:
  - schema version;
  - package ID;
  - goal ID;
  - runtime spec digest;
  - validation intent;
  - claim graph;
  - proof obligations;
  - validator specs;
  - task sets;
  - fixture refs;
  - evidence contract;
  - scoring projection;
  - authority policy;
  - leakage policy;
  - abstention policy;
  - public view hash;
  - sealed view hash;
  - package hash;
  - frozen flag.
- Hashing must close over public and sealed projections plus referenced sealed artifacts.
- Freeze should be a pure helper that computes and returns a validated package. Avoid model validators that recursively compute hashes in a way that depends on partially-filled fields.
- Parent/child comparisons must be blocked unless both evaluations use the same `oracle_package_hash`.

Tests:

- package hash stable across object/dict round trips;
- changing public task text changes public/package hash;
- changing private expected value changes sealed/package hash;
- package with missing hard-claim validator fails QA;
- package with no tasks fails QA unless explicitly marked diagnostic-only.

### Public And Sealed Projections

Use Stack's approach because it aligns with existing benchmark privacy helpers.

Required corrections:

- `oracle_runtime_visible_tasks_by_partition(package, partition)` must use `runtime_visible_benchmark_task(...)`.
- `oracle_tasks_by_partition(package, partition)` must return evaluator-side sealed tasks.
- Public projection must not include:
  - `private_expected`;
  - `expected` for private-answer tasks;
  - hidden fixture refs;
  - private rubrics;
  - promotion thresholds;
  - sealed validator inputs;
  - field names such as `sealed_inputs` when the rendered public payload is inspected.
- Public projection may include:
  - task IDs;
  - public prompts;
  - allowed tool categories;
  - public metadata;
  - public validator summaries;
  - authority ceilings/caps only if they do not reveal thresholds or sealed mechanics.

Tests:

- public projection rendered as JSON contains none of the forbidden keys;
- runtime-visible task has `private_expected is None`, `expected is None`, `verification_required is False` when a private expected value exists;
- sealed evaluator task preserves private expected value;
- public and sealed hashes differ when sealed content exists.

### OracleCompiler

Use Stack's compiler structure as the base, but do not land it as a fake all-purpose LLM compiler in the first patch.

Implementation order:

1. Deterministic compiler first.
2. Provider/LLM compiler hook second, behind an explicit flag.
3. Adaptive compiler graph last.

Required behavior:

- Input: `GoalSpec`, optional `RuntimeSpec`, optional prior ledgers.
- Output: frozen `OraclePackage`.
- The default deterministic compiler must emit a non-vacuous package:
  - at least one task set;
  - at least one hard claim;
  - at least one validator for each hard claim;
  - an evidence contract compatible with current `ProgressOracle`.
- The first deterministic package should wrap existing demo/tool-frontier behavior before trying to be clever.
- Domain hints can select families:
  - repo/coding goal -> `repo_patch`, `schema_artifact`, `trace_state`;
  - service/API goal -> `stateful_service`, `consent_proof`, `trace_state`;
  - trading/finance goal -> `trading_outcome`, `schema_artifact`, `trace_state`;
  - factual/research goal -> `factual_grounded`, `schema_artifact`, `pairwise_preference` with authority caps.
- Hidden-answer/exact-private-answer is only one validator family and must not become the whole product story.
- If no trustworthy validation authority exists, the compiler must produce an abstaining/diagnostic package rather than promotion-grade evidence.

Tests:

- compiler emits a valid package for a generic goal;
- compiler emits repo-patch validators for repo-patch goals;
- compiler emits trading outcome validators for trading goals without adding trading assumptions to non-trading goals;
- compiler emits consent proof only for side-effect/service goals;
- QA rejects vacuous output.

### Validator Registry And Families

Use Stack's runnable family API. Do not use Patch's inert `build_specs`-only family bodies.

Required family interface:

```python
class ValidatorFamily:
    family_id: str
    authority_ceiling: str
    default_visibility: Literal["public", "private", "sealed"]
    default_failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"]
    input_contract: dict[str, Any]
    output_schema: dict[str, Any]
    leakage_risks: list[str]
    health_tests: list[dict[str, Any]]
    def score_applicability(context: dict[str, Any]) -> float: ...
    def make_spec(...): ...
    def run_validator(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult: ...
```

Initial families:

- `exact_private_answer`: high-authority canary, not general oracle.
- `schema_artifact`: validates JSON/files/artifacts against schema.
- `repo_patch`: validates patch apply, public tests, hidden tests, and tamper evidence.
- `stateful_service`: validates expected final state and duplicate/forbidden side effects.
- `trace_state`: validates required/forbidden trace events, budgets, receipts.
- `pairwise_preference`: weak authority unless calibrated and paired with stronger checks.
- `factual_grounded`: citation/freshness/contradiction checks, authority capped below sealed tests.
- `trading_outcome`: cutoff, order validity, fill reconciliation, portfolio state, cost/risk, post-close snapshots.
- `consent_proof`: side-effect consent and receipt validation.
- `human_audit`: optional high-authority human-signed result.

Required corrections:

- Validator execution exceptions must produce `ValidatorResult(status="error")`; do not silently drop them.
- Weak model/human preference validators cannot promote alone.
- Each family needs one positive and one negative control test.

### OracleEvaluationRunner

Use Stack's `agintor/evaluation/oracle_runner.py` concept.

Required behavior:

- Input: frozen `OraclePackage`, `RunResult` or run payload, sealed evaluator payload.
- For each `ValidatorSpec`, call the matching `ValidatorFamily`.
- Emit `ValidatorResult` with:
  - validator ID;
  - family ID;
  - claim IDs;
  - status;
  - authority used;
  - observations;
  - evidence digest.
- Aggregate `ClaimResult`:
  - pass only when the claim has sufficient non-error evidence at the required authority;
  - fail on hard validator failure;
  - abstain when evidence is missing or below authority floor.
- Return errors as structured evidence, not exceptions, unless the package itself is invalid.

Tests:

- runner emits validator and claim results;
- validator exception becomes error result;
- hard claim fails when validator fails;
- unsupported family abstains/errors with A0 authority;
- result digests are stable.

### RuntimeSpec V2 And LangGraph Runtime

Use Stack's runtime generation/export/deployment intent, but merge Patch's real `StateGraph` path.

Current official LangGraph Python docs still use:

```python
from langgraph.graph import StateGraph
```

and require compiling the builder before invoking it. The implementation should use that path when the optional dependency is installed and fall back to deterministic sequential execution only when it is not.

Required architecture:

- Split solve-time modules from factory/export modules:
  - solve-time: `agintor/runtime/langgraph/state.py`, `operation_service.py`, `executor.py`, `adapters.py`, `entrypoint.py`;
  - factory/export-time: `agintor/runtime/langgraph/compiler.py`.
- The generated runtime app must import solve-time modules only.
- `compiler.py` may call `bundle_runtime_kernel(...)`; solve-time modules must not import factory/search/evaluation code.
- Add `runtime/langgraph` solve-time modules to the runtime kernel bundle.
- Add `contracts/runtime_spec.py` to the runtime kernel bundle and generated contract shim.
- Do not put `contracts/oracle.py` into exported runtime unless only a public projection type is needed. The sealed oracle package must stay factory/evaluator-side.
- Generated v2 runtimes must be self-contained under the existing runtime protocol.

LangGraph execution behavior:

- If `langgraph` is installed:
  - create a `StateGraph` using a TypedDict or Pydantic-compatible state schema;
  - add nodes from `RuntimeSpec.graph.nodes`;
  - add edges from `RuntimeSpec.graph.edges`;
  - use condition/routing only for the subset implemented in pass 1;
  - compile and invoke the graph.
- If `langgraph` is not installed:
  - use the deterministic sequential executor;
  - report backend as `sequential_fallback`;
  - keep tests green without optional extras.

Runtime operation behavior:

- `RuntimeOperationService` should dispatch node types:
  - `direct_response`;
  - `agent`;
  - `builtin`;
  - `tool`;
  - `merge`;
  - `verify`;
  - `service_action`;
  - `repo_patch`.
- Side-effect nodes must produce receipts with idempotency keys and action fingerprints.
- Tool nodes may only call runtime-visible tools.
- Host-gated side effects must record intent and require host execution/receipt.

Host/runtime protocol integration:

- Do not bypass `RuntimeHost`.
- `load_runtime(...)` should load v2 manifests and attach `runtime_spec`.
- `LoadedRuntime` should either expose `runtime_spec` or a v2 executor in addition to the current policy objects.
- `TaskRuntime.run_task(...)` or an owning runtime-kernel mixin should detect `runtime_spec` and delegate execution to the v2 executor while still producing normal `RunResult`, trace, checkpoint, and side-effect receipt surfaces.
- The existing local and docker host backends must continue launching `agintor_runtime.runtime_entry`.
- Checkpoint integration should embed LangGraph state inside the existing `CheckpointEnvelope`, not add a new checkpoint ABI.

Template placement:

- If keeping Patch's static template, put it under `agintor/templates/baseline_runtime_v2/`, not repo-root `templates/`.
- Use the dynamic `RuntimeSpecCompiler` as canonical.
- Keep the static template as a fixture/reference only.

Dependency plan:

- Add optional extras only:
  - `langgraph = ["langgraph>=1,<2"]`;
  - add `langchain>=1,<2` only when generated agent nodes actually use LangChain agent helpers;
  - keep Inspect/OpenAI eval runners optional and lazy.
- The base install must not require LangGraph.

Tests:

- v2 runtime compiles without optional LangGraph and runs sequential fallback.
- v2 runtime compiles with LangGraph when extra is installed.
- v2 runtime solves a prompt through `RuntimeHost`, not just direct executor invocation.
- v2 runtime solves a benchmark task through `RuntimeHost`.
- runtime hash changes when `runtime_spec.json` changes.
- exported runtime contains no sealed oracle package.
- exported runtime contains runtime spec, generated app, runtime kernel, deployment contract, and runtime manifest.

### Runtime Kernel Bundling

Stack's v2 compiler exposed a real problem: clean worktrees do not have ignored `agintor/templates/baseline_runtime/` files, but `bundle_runtime_kernel(...)` currently requires `templates/baseline_runtime/runtime_profile.json`.

Required fix:

- Do not commit ignored `agintor/templates/baseline_runtime/` generated files.
- Make kernel bundling deterministic in clean worktrees:
  - either move required runtime resource defaults into tracked package data outside the ignored generated template;
  - or generate the required runtime profile resource during export before bundling;
  - or teach `bundle_runtime_kernel(...)` to synthesize the default runtime profile payload when the ignored template file is absent.
- Add a clean-worktree test for v2 export that does not depend on ignored local files.

Also update kernel bundling so exported v2 runtimes include:

- `contracts/runtime_spec.py`;
- generated contract shim exports for `RuntimeSpec`;
- solve-time `runtime/langgraph` modules;
- no factory/search/evaluation/oracle sealed modules.

### Evaluator Integration

Use Stack's integration intent, but rewrite the patch by hand.

Touch:

- `agintor/evaluation/benchmarks.py`
- `agintor/evaluation/evaluator.py`
- `agintor/evaluation/oracle_runner.py`
- `agintor/contracts/evidence.py`
- `agintor/evaluation/progress_oracle.py`

Required behavior:

- `BenchmarkSuite` may carry an optional `oracle_package`, but do not make this required for existing suites.
- `RuntimeEvaluator` accepts `oracle_package: OraclePackage | str | Path | None`.
- If `oracle_package` is present:
  - load and freeze/validate it;
  - run QA before candidate evaluation;
  - use runtime-visible public tasks for candidate execution;
  - retain sealed tasks and sealed payloads for evaluator-side scoring;
  - run `OracleEvaluationRunner` per run;
  - write validator and claim results into `EvidenceRecord`.
- Evidence identity fields:
  - `oracle_package_hash`;
  - `oracle_public_view_hash`;
  - `oracle_sealed_view_hash`;
  - `runtime_spec_digest`;
  - validator result digests;
  - claim result digests.
- `PairedComparison`, `ProgressSignal`, and `PromotionDecision` must carry package/spec identity.
- `ProgressOracle` remains the promotion gate.
- `ProgressOracle` must reject/abstain/quarantine on:
  - missing oracle package for package-required evaluation;
  - package hash mismatch between parent and child;
  - failed oracle package QA;
  - failed leakage checks;
  - missing hard validator evidence.

Do not swallow `OracleEvaluationRunner` failures. Convert per-validator failures to `ValidatorResult(status="error")`; package-level invalidity should fail/abstain the evaluation explicitly.

Tests:

- current WS4 `ProgressOracle` tests still pass for no-package existing behavior;
- evaluator with package writes package/spec identity;
- candidate never receives sealed fields;
- package QA failure blocks promotion;
- parent/child package hash mismatch blocks comparison;
- validator/claim results appear in evidence ledger.

### Factory And Planning Integration

Do not use Stack's stale `factory/planning.py` search/replace blocks.

Touch:

- `agintor/factory/planning.py`
- `agintor/factory/export.py`
- `agintor/contracts/factory.py` only if summaries need new refs
- `agintor/evaluation/benchmarks.py`

Required behavior:

- Planning should build a `GoalSpec` first.
- `BenchmarkPlan` remains task-selection projection.
- `VerifierBundle` remains a projection/companion, not the root validation contract.
- Add an `OracleCompiler` call at the point where a frozen goal-conditioned validation package can be created.
- Store package refs/hashes in build summaries/factory artifacts.
- Exported v2 runtime includes:
  - `runtime_manifest.json`;
  - `runtime_spec.json`;
  - generated runtime app;
  - deployment contract;
  - runtime kernel bundle;
  - public validation summary if useful.
- Exported v2 runtime must not include:
  - sealed oracle package;
  - private fixtures;
  - private expected values;
  - hidden tests;
  - private rubrics;
  - promotion thresholds.
- Factory/evaluator-side artifacts may store the sealed oracle package under `.agintor_runs/` or a build artifact dir, but not inside exported runtime.

Tests:

- `build-runtime` can create a v2 runtime with package hash refs;
- exported runtime load succeeds in a clean worktree;
- exported runtime does not contain sealed oracle files;
- factory build summary exposes public package hash/provenance.

### Runtime Loader

Do not copy Stack's loader diff. It produced an indentation error.

Required behavior:

- `RuntimeManifest` loads existing v1 manifests with default `runtime_kind="policy_modules_v1"`.
- For v1:
  - preserve current policy module import behavior exactly.
- For v2:
  - load `runtime_spec.json` from `runtime_spec_path`;
  - validate `runtime_spec_digest` if present;
  - compute runtime identity including normalized spec digest and generated app digest;
  - create compatibility policy objects only if existing `TaskRuntime` still requires topology/memory/tool/control slots;
  - avoid importing non-existent generated policy classes.
- `LoadedRuntime` should expose `runtime_spec: RuntimeSpec | None`.

Tests:

- current baseline v1 runtime still loads;
- v2 runtime loads;
- bad `runtime_spec_digest` fails closed;
- v2 runtime hash changes when spec changes;
- v1 behavior is unchanged.

### Search And Archive Integration

Use Stack's intent, but rebase onto current `search/engine.py`, `search/archive.py`, and `contracts/search.py`.

Touch:

- `agintor/search/spec_mutator.py`
- `agintor/search/engine.py`
- `agintor/search/archive.py`
- `agintor/contracts/search.py`
- `agintor/contracts/evidence.py`

Required behavior:

- Detect v2 parents by loaded manifest/runtime spec.
- For v1 parents, preserve current mutator behavior.
- For v2 parents:
  - load parent `RuntimeSpec`;
  - propose typed `SpecAction`s;
  - apply actions to create child spec;
  - compile child runtime directory with `RuntimeSpecCompiler`;
  - write mutation ledger;
  - evaluate parent and child under the same frozen `OraclePackage`;
  - attach action IDs to Stage 4 decision reason codes or structured fields;
  - only update archive/scheduler/predictors when `ProgressOracle` permits.
- Archive entries and evolution history rows should include:
  - `runtime_spec_digest`;
  - `parent_runtime_spec_digest`;
  - `child_runtime_spec_digest`;
  - `oracle_package_hash`;
  - `mutation_action_ids`;
  - `evidence_digest`.

Tests:

- v2 stage 0 uses spec actions, not Python patch mutation;
- child runtime spec digest differs after mutation;
- mutation ledger is written;
- promotion decision references action IDs;
- package hash mismatch prevents archive insertion;
- v1 search tests continue to pass.

### CLI

Use Stack's CLI intent, but resolve the ambiguous `runtime_backend` anchor manually.

Commands:

```bash
agintor init-runtime <dest> --runtime-kind policy_modules_v1
agintor init-runtime <dest> --runtime-kind langgraph_spec_v2
agintor inspect-oracle <package_dir> [--public]
agintor oracle-qa <package_dir>
agintor eval <runtime_dir> --suite demo --oracle-package <package_dir>
```

Rules:

- Existing command defaults must not change.
- v2 init should call `RuntimeSpecCompiler`, not copy a root-level `templates/` directory.
- `eval --oracle-package` should pass the package to `RuntimeEvaluator`.
- CLI must not print sealed package contents unless the user explicitly asks for a sealed evaluator-side inspection command. Default `inspect-oracle` should be public-safe or require `--sealed`.

Tests:

- `init-runtime` v1 default still works;
- `init-runtime --runtime-kind langgraph_spec_v2` writes manifest/spec/kernel;
- `oracle-qa` returns JSON report;
- `inspect-oracle --public` contains no sealed fields;
- `eval --oracle-package` wires package into evaluator.

### TradingAgents

Use Stack's TradingAgents adapter as an example, not the generic oracle.

Required behavior:

- `TradingAgentsRuntimeSpec` is a `RuntimeSpec` profile or adapter output.
- TradingAgents is selected only for finance/trading goals or explicit user choice.
- Trading validator evidence covers:
  - data cutoff;
  - order validity;
  - fill reconciliation;
  - portfolio reconciliation;
  - cost/slippage;
  - risk policy;
  - post-close outcome snapshot.
- Trading-specific validators must not appear for non-trading goals.
- TradingAgents external dependency must remain optional.

Tests:

- trading goal selects `trading_outcome`;
- non-trading goal does not select `trading_outcome`;
- trading ledger validator passes positive control and fails negative control;
- adapter emits valid `RuntimeSpec`.

## Concrete Implementation Phases

### Phase 0: Prep And Rebase Setup

Read first:

- `AGENTS.md`
- `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
- `implementation_workstreams/WORKSTREAM_4_BENCHMARKS_EVALUATION_AND_SEARCH.md`
- `implementation_workstreams/WORKSTREAM_5_TOOLING_PROVIDERS_AND_CONTROL.md`
- `LangGraph and Oracle Refactor Plan pass 1.md`
- `agintor_full_oracle_langgraph_stack/README.md`
- `agintor_full_plan_patch/README.md`

Rules:

- Do not apply either patch pack directly.
- Do not copy `.pyc` or `__pycache__`.
- Stage changes by phase.
- Keep v1 behavior green after every phase.
- Use focused tests; do not start with full pytest.

### Phase 1: Contracts And Hashing

Implement:

- `agintor/contracts/runtime_spec.py`
- `agintor/contracts/spec_actions.py`
- `agintor/contracts/oracle.py`
- exports in `agintor/contracts/__init__.py`
- forward-ref rebuilds if needed

Modify:

- `agintor/contracts/runtime.py`
- `agintor/contracts/evidence.py`
- `agintor/contracts/search.py`

Exit gate:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_runtime_spec.py tests/test_spec_actions.py tests/test_oracle_package.py -q
```

Do not continue until digest stability and package hashing are correct.

### Phase 2: Projections, Package IO, QA

Implement:

- `agintor/oracle/package_io.py`
- `agintor/oracle/projections.py`
- `agintor/oracle/qa.py`

Required tests:

- `tests/test_oracle_public_projection.py`
- `tests/test_oracle_sealed_eval.py`
- QA failure tests for vacuity, leakage, missing hard claim coverage, invalid hash.

Exit gate:

```powershell
.\.venv\Scripts\python -m pytest tests/test_oracle_package.py tests/test_oracle_public_projection.py tests/test_oracle_sealed_eval.py -q
```

### Phase 3: Deterministic OracleCompiler And Runnable Validators

Implement:

- `agintor/oracle/compiler.py`
- `agintor/oracle/validator_registry.py`
- `agintor/oracle/families/*.py`
- `agintor/evaluation/oracle_runner.py`

Use Stack's runnable family design.

Do not implement the LLM compiler as default behavior in this phase. Keep provider hooks inert/optional until QA is strong.

Exit gate:

```powershell
.\.venv\Scripts\python -m pytest tests/test_oracle_qa.py tests/test_validator_registry.py tests/test_oracle_evaluation_runner.py tests/test_trading_oracle_package.py -q
```

### Phase 4: Evaluator Integration With No Runtime Substrate Change

Modify:

- `agintor/evaluation/benchmarks.py`
- `agintor/evaluation/evaluator.py`
- `agintor/evaluation/progress_oracle.py`
- evidence ledgers and Stage 4 writer paths

Goal:

- Existing runtime still executes exactly as before.
- Evaluator can optionally use an oracle package for public task projection, sealed validation, and evidence identity.

Exit gate:

```powershell
.\.venv\Scripts\python -m pytest tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py tests/test_pairwise_comparator.py -q
.\.venv\Scripts\python -m pytest tests/test_oracle_public_projection.py tests/test_oracle_sealed_eval.py tests/test_oracle_evaluation_runner.py -q
```

### Phase 5: RuntimeSpec V2 Execution Path

Implement:

- `agintor/runtime/langgraph/state.py`
- `agintor/runtime/langgraph/operation_service.py`
- `agintor/runtime/langgraph/executor.py`
- `agintor/runtime/langgraph/adapters.py`
- `agintor/runtime/langgraph/entrypoint.py`
- `agintor/runtime/langgraph/compiler.py`

Modify:

- `agintor/runtime/loader.py`
- `agintor/runtime/sdk/bundle.py`
- runtime kernel entrypoint or owning mixin to route v2 execution
- `pyproject.toml` optional extras

Lift from Patch:

- lazy `StateGraph` import and fallback structure.

Lift from Stack:

- generated manifest/deployment/kernel bundling intent;
- operation service dispatch;
- compatibility adapter shape.

Exit gate:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_langgraph_runtime_compiler.py tests/test_runtime_host.py -q
```

Add a focused v2 runtime host test before considering this phase complete.

### Phase 6: Factory Export And CLI

Modify:

- `agintor/factory/planning.py`
- `agintor/factory/export.py`
- `agintor/cli.py`
- factory build summary contracts only if needed

Add:

- optional `agintor/templates/baseline_runtime_v2/` reference fixture, if useful.

Exit gate:

```powershell
.\.venv\Scripts\python -m pytest tests/test_runtime_builder.py tests/test_runtime_host.py -q
```

Manual smoke:

```powershell
.\.venv\Scripts\python -m agintor.cli init-runtime .tmp_rt_v2 --runtime-kind langgraph_spec_v2
.\.venv\Scripts\python -m agintor.cli solve .tmp_rt_v2 --prompt "Return JSON with key answer"
```

Use the repo's actual CLI invocation style if it differs.

### Phase 7: Search, Archive, SpecActionMutator

Implement:

- `agintor/search/spec_mutator.py`

Modify:

- `agintor/search/engine.py`
- `agintor/search/archive.py`
- `agintor/contracts/search.py`

Exit gate:

```powershell
.\.venv\Scripts\python -m pytest tests/test_spec_mutator.py tests/test_evolution_engine_search.py tests/test_promotion_search_routing.py -q
```

Required proof:

- v2 mutation writes action ledger.
- v2 parent and child are evaluated under same package hash.
- archive insertion is blocked when evidence is missing or incomparable.

### Phase 8: TradingAgents Example Profile

Implement after the generic oracle and runtime path are green.

Add:

- `agintor/integrations/tradingagents/*`
- `tests/test_tradingagents_adapter.py`
- `tests/test_trading_oracle_package.py`

Exit gate:

```powershell
.\.venv\Scripts\python -m pytest tests/test_tradingagents_adapter.py tests/test_trading_oracle_package.py -q
```

### Phase 9: Final Validation

Focused sequence:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_runtime_spec.py tests/test_spec_actions.py -q
.\.venv\Scripts\python -m pytest tests/test_oracle_package.py tests/test_oracle_public_projection.py tests/test_oracle_sealed_eval.py tests/test_oracle_qa.py tests/test_oracle_evaluation_runner.py tests/test_validator_registry.py -q
.\.venv\Scripts\python -m pytest tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py tests/test_pairwise_comparator.py -q
.\.venv\Scripts\python -m pytest tests/test_langgraph_runtime_compiler.py tests/test_runtime_host.py tests/test_runtime_execution.py -q
.\.venv\Scripts\python -m pytest tests/test_spec_mutator.py tests/test_evolution_engine_search.py tests/test_promotion_search_routing.py -q
.\.venv\Scripts\python -m pytest tests/test_runtime_builder.py tests/test_tradingagents_adapter.py tests/test_trading_oracle_package.py -q
git diff --check
```

Only then consider broader pytest. On this Windows host, prefer per-file or per-slice commands and workspace-local `--basetemp` for broad runs.

## Definition Of Done

Pass 1 is complete only when:

1. `RuntimeSpec` represents a built runtime genome with stable digesting.
2. `SpecAction` mutates v2 runtimes and writes a mutation ledger.
3. v2 runtimes run through the existing host/runtime protocol.
4. Optional LangGraph dependency is used when installed; deterministic fallback works when absent.
5. `OraclePackage` is created from `GoalSpec` and frozen before evaluation.
6. Public and sealed projections are separate and tested.
7. Candidate runtimes never see sealed/private validation material.
8. Evaluator runs validators and records `ValidatorResult` / `ClaimResult`.
9. Evidence records carry package hash, validator IDs, claim IDs, runtime spec digest, and evidence digest.
10. Parent/child comparison requires the same oracle package hash.
11. `ProgressOracle` remains the only promotion gate.
12. Search/archive update only when promotion decision allows it.
13. TradingAgents exists as one profile/family, not the generic oracle.
14. Existing v1 runtime behavior still works.
15. No ignored/generated `.pyc`, `__pycache__`, or sealed oracle artifacts are committed.

## Non-Goals For This Pass

- No visual graph UI.
- No broad `RuntimeHost` replacement.
- No new runtime contract version.
- No checkpoint/storage migration layer.
- No live mutation of LangChain/LangGraph object instances.
- No LLM judge as final promotion authority.
- No trading-only oracle.
- No sealed oracle package in exported runtimes.
- No toy validators that can promote production evidence.

## Practical Patch Application Guidance

Use this mapping:

```text
Stack primary:
  new_files/agintor/contracts/oracle.py
  new_files/agintor/contracts/runtime_spec.py
  new_files/agintor/contracts/spec_actions.py
  new_files/agintor/evaluation/oracle_runner.py
  new_files/agintor/oracle/*
  new_files/agintor/runtime/langgraph/*
  new_files/agintor/search/spec_mutator.py
  new_files/agintor/integrations/tradingagents/*

Patch secondary:
  new_files/agintor/runtime/langgraph/compiler.py StateGraph fallback pattern
  new_files/templates/baseline_runtime_v2/runtime_spec.json, moved to agintor/templates/baseline_runtime_v2/
  new_files/templates/baseline_runtime_v2/langgraph_app.py, as fixture/reference only
  EXISTING_FILE_EDITS.search_replace.md as a cleaner checklist of current-repo anchors
```

Never copy:

```text
agintor_full_oracle_langgraph_stack/**/__pycache__/
agintor_full_oracle_langgraph_stack/**/*.pyc
agintor_full_plan_patch/write_patch_files*.py
repo-root templates/baseline_runtime_v2/
sealed oracle package files into exported runtimes
```

Manual rebase order:

1. Contracts.
2. Oracle package IO/projection/QA.
3. Deterministic compiler and validators.
4. Evaluator evidence integration.
5. Runtime v2 host execution.
6. Factory/CLI.
7. Search/archive.
8. TradingAgents.

Do not start with the runtime migration alone. The runtime substrate and oracle substrate must land as a narrow vertical slice because the validation signal is the central product problem.
