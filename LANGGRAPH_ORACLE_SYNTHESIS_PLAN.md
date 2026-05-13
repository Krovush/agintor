# LangGraph + Oracle Refactor — Synthesis Implementation Plan

This is the actionable build plan for landing the LangGraph runtime + adaptive `OracleCompiler` pass-1 work into the Agintor MVP. It supersedes the two raw patch bundles in the repo root (`agintor_full_plan_patch/`, `agintor_full_oracle_langgraph_stack/`). **Neither bundle ships a working pipeline; this plan is a corrected synthesis you implement against the live repo.**

The source-of-truth design plan is `LangGraph and Oracle Refactor Plan pass 1.md`. The architectural directives in `CLAUDE.md` (no toy demos, no fallbacks, no MVP-checkpoint compatibility shims, four-layer factory/host/runtime-kernel/policy boundary, single `RUNTIME_CONTRACT_VERSION`) bind everything below.

---

## 0. TL;DR for the implementing agent

- **Base bundle:** `agintor_full_oracle_langgraph_stack/` ("Stack"). It is the more complete and integrated implementation but does not apply cleanly and has real defects.
- **Reference bundle:** `agintor_full_plan_patch/` ("Patch"). Cleaner skeletons, narrower edits, missing the runner half. Lift two specific things from it; ignore the rest.
- **Posture:** treat both bundles as raw material. Do not run either apply script blindly. Every existing-file edit is re-verified against the live repo before it lands.
- **What is "done":** the ten-item completion checklist in §16 of the source plan, narrowed to the regression slice in §11 of this document.
- **Workstream:** WS4 (per `CLAUDE.md`). Items that fall in WS3 (state, durability) are explicitly deferred — see §13.

---

## 1. Source-of-truth decision

Per-layer winner. Where Stack wins it is the base; where Patch wins, lift the named file or pattern verbatim.

| Layer | Winner | Reason | Action |
|---|---|---|---|
| `agintor/contracts/runtime_spec.py` | Stack | Richer node types (`direct_response`, `builtin`, `service_action`, `repo_patch`), `PromptSpec`, `scope` per agent, structured graph validation. | Copy Stack file. Then fix `MutationActionRef.created_at` digest leak (§3.1). |
| `agintor/contracts/spec_actions.py` | Stack | Has the typed action-type enum the plan calls for and the validation hooks the search engine needs. | Copy Stack file. Verify `action_id` stability across roundtrips (§3.1). |
| `agintor/contracts/oracle.py` | Stack | `OracleTask` wraps the existing `BenchmarkTask` so the host already knows how to project it. Auto-derives `package_hash`/`public_view_hash`/`sealed_view_hash` in `model_validator`. Refuses to construct a package with uncovered hard claims. | Copy Stack file. Then fix the `BenchmarkTask.expected` integration error (§3.2). |
| `agintor/oracle/projections.py` | Stack-pattern | Stack's projection lives in `contracts/oracle.py` (`oracle_public_projection`); Patch's standalone module leaks sealed validator metadata (`validator_id`, `family_id`, `claim_ids`, `independence_group`). | Use Stack's projection logic. Re-export it from `agintor/oracle/projections.py` so call sites have a stable import path. Drop Patch's `_strip_private` walker. |
| `agintor/oracle/qa.py` | Stack | Larger and validates leakage by re-running projections + recomputing hashes. | Copy Stack file. |
| `agintor/oracle/package_io.py` | Patch | Stack writes only the sealed package; Patch writes both projections + a manifest. | Copy Patch file. Adjust to call Stack's `oracle_public_projection`. |
| `agintor/oracle/validator_registry.py` | Stack | Registry exposes `family.run_validator(spec, payload)` and `family.make_spec(...)`. Patch's `build_specs`/`can_handle` model is a declaration-only API that leaves validators inert. | Copy Stack file. |
| `agintor/oracle/families/*.py` | Stack | Every family in Stack has a `_run(spec, payload) -> ValidatorResult` body. Patch's families only emit specs. | Copy all 11 Stack family files including `consent_proof.py`. Discard Patch's family bodies entirely. |
| `agintor/oracle/compiler.py` | Stack | Patch compiler accepts `task_sets=()` and emits packages with **zero tasks**, which trips QA. Stack compiler emits real default tasks with claim coverage and routes claims to families per detected domain. | Copy Stack file. Then make the LLM provider hook real (§3.3) and gate domain-specific claim packs behind capability detection rather than substring matching, so adding a new domain does not require editing the compiler core. |
| `agintor/oracle/compiler_graph.py` | Stack | Larger LangGraph workflow shell with proper subagent dispatch placeholders. | Copy Stack file. |
| `agintor/oracle/subagents.py` | Stack | Larger, names the subagents the §6.2 plan lists. | Copy Stack file. |
| `agintor/evaluation/oracle_runner.py` | Stack-only | **Patch does not have this file.** Without it, validators never execute and `ClaimResult`s are never produced. | Copy Stack file. This is the single biggest reason Patch is non-viable. |
| `agintor/runtime/langgraph/state.py` | Stack | Pydantic state model, not a dict, so the LangGraph adapter and the sequential walker share types. | Copy Stack file. |
| `agintor/runtime/langgraph/operation_service.py` | Stack | Real dispatcher: agent / builtin / tool / merge / verify / service_action / repo_patch. Records side-effect receipts with idempotency keys and fingerprints. Patch has a thin Protocol shim that does nothing. | Copy Stack file. |
| `agintor/runtime/langgraph/compiler.py` | **Hybrid** | Stack's `RuntimeSpecCompiler.compile_to_directory` correctly writes spec + generated app + `RuntimeManifest` v2 + `DeploymentContract` and bundles the runtime kernel. **But Stack's `CompiledSpecRuntime` is a hand-rolled sequential walker that never constructs a `langgraph.graph.StateGraph`** — the plan explicitly requires LangGraph as the substrate. Patch's compiler has the lazy-import + `StateGraph` build path that Stack lacks. | Start from Stack's file. Inside `CompiledSpecRuntime.invoke` (or behind a new `LangGraphBackend` strategy class), add Patch's lazy-import pattern: `try: from langgraph.graph import StateGraph` → build the real graph → fall through to the sequential walker only when the optional dep is missing. Surface the chosen backend on `CompiledSpecRuntime.backend`. |
| `agintor/runtime/langgraph/adapters.py` | Stack | Has `load_runtime_spec` and `build_spec_policy_objects` referenced by the loader edit. | Copy Stack file. Verify both symbols exist before the loader diff lands. |
| `agintor/runtime/langgraph/checkpointing.py` | Stack | Both are thin; Stack has the right shape for embedding into `CheckpointEnvelope` (per source plan §2). | Copy Stack file. |
| `agintor/runtime/langgraph/entrypoint.py` | Stack-only | Bridges the spec runtime back into the existing host protocol. Patch lacks this; without it the v2 runtime cannot be invoked through `agintor solve`. | Copy Stack file. Wire it from the generated runtime manifest's `policy_modules` dict. |
| `agintor/search/spec_mutator.py` | Stack-shape | Both implement `SpecActionMutator`. Stack's heuristic + provider variants line up with the search engine edit. Patch's variant is named `SpecMutationContext` only and does not pair with an evaluator integration. | Copy Stack file. |
| `agintor/integrations/tradingagents/*` | **Hybrid** | Stack's `adapter.py`, `data_snapshots.py`, `ledgers.py`, `action_mapper.py` are larger and integrate with the validator runner. Patch's `compiler.py` is more substantial and implements the trading runtime spec compilation Stack handles in 4 lines. | Take Stack files for everything *except* `compiler.py`; for `compiler.py`, take Patch's version and rewire it to consume Stack's `TradingAgentsRuntimeSpec` profile. |
| `agintor/oracle/families/trading_outcome.py` | Stack | Has runner. | Copy Stack file. |
| `templates/baseline_runtime_v2/` | Patch | Patch ships a concrete `runtime_spec.json` and `langgraph_app.py`. Stack only ships a README and depends on `baseline_langgraph_runtime_spec()` to materialize the template at runtime. | Ship the Patch JSON + app file. **Reconcile field names with Stack contracts first** — Stack uses `entry_node` and `terminal_nodes`; Patch's JSON uses `entry_node_id` and `terminal_node_ids`. Use Stack's names since the contracts are Stack's. |

### Things to *not* take from either bundle

- `agintor_full_plan_patch/write_patch_files*.py` — these are emission scripts the bundle generator used internally, not artifacts.
- `agintor_full_oracle_langgraph_stack/__pycache__/`, `agintor_full_oracle_langgraph_stack/new_files/**/__pycache__/` — pre-compiled bytecode pollution. Filter these out when copying.
- `agintor_full_plan_patch/EXISTING_FILE_EDITS.search_replace.md` — the Patch existing-edit map is shallower than the Stack one and does not close the loop on factory/search/CLI. Use Stack's individual `existing_edits/*.diff` files as the **starting point** for §4 below, but verify every anchor by hand.

---

## 2. Architecture invariants (from source plan §4–§13)

Every change must keep these intact. If a synthesis decision violates one, revisit the decision.

1. **Factory / host / runtime-kernel / policy boundary** stays separated. Factory code (`runtime_builder`, `goal_rubric`, `evolution`, `evaluator`, `mutator`, `archive`) calls the runtime via the protocol entrypoint or `runtime_api.py`, never `task_runtime/*` internals. The new `agintor/oracle/*` and `agintor/runtime/langgraph/*` packages live on the factory side; only the *generated* app code in `runtime/langgraph/entrypoint.py` and the bundled kernel run on the host/runtime-kernel side.
2. **Single `RUNTIME_CONTRACT_VERSION`.** No new ABI/storage version axes. No legacy migration code for existing checkpoints, traces, or exported runtimes — they are disposable per `CLAUDE.md`.
3. **One `ExecutionPlan`.** Both prompt solves and benchmark solves still compile to one plan. The `langgraph_spec_v2` path adds a second runtime *kind*, not a second plan format.
4. **`ProgressOracle` stays the promotion authority.** The `OracleCompiler` produces frozen packages; it does not decide promotion. Validator runners produce evidence; they do not decide promotion.
5. **Exported runtime never carries sealed oracle material.** `oracle_public_view_hash`, the sealed package, private fixtures, hidden tests, promotion thresholds, private rubrics, and oracle compiler traces that reveal private authority are evaluator-only.
6. **Pairwise comparisons require matching `oracle_package_hash`.** A child evaluated under a new package versus a parent evaluated under an old package is a `quarantine`, not a promotion.
7. **TradingAgents is one runtime profile and one validator family.** It is not the root oracle and never gets to define the validation core for non-trading goals.
8. **Optional deps stay optional.** `langgraph`, `langchain`, `langsmith`, `inspect-ai` are added under `pyproject.toml` `[project.optional-dependencies]` (Stack's `pyproject` diff is correct). The deterministic sequential fallback path must keep `python -m pytest` green when those extras are not installed.

---

## 3. Defects in the bundles to fix during synthesis

The other agent ran the bundles and reported test failures. I confirmed the structural causes from source. Each item below is a concrete fix you must apply on top of the copy operations in §1.

### 3.1 `RuntimeSpec` digest instability

**Symptom:** `runtime_spec_digest(spec)` returns a different value on `model_validate(json.dumps(spec))` roundtrip.

**Root cause:** Both bundles include `created_at: float = Field(default_factory=now_ts)` on `MutationActionRef`. When a spec is dumped and reloaded with no `created_at`, `default_factory` re-fires and the digest changes.

**Fix:**
- Make `created_at` excluded from canonical-payload calculation. The simplest fix is to define a private helper:
  ```python
  _DIGEST_EXCLUDE = {"created_at", "spec_digest", "parent_spec_digest", "metadata"}
  def _spec_digest_payload(spec: RuntimeSpec) -> dict[str, Any]:
      payload = spec.model_dump(mode="json", exclude_none=True, exclude={"spec_digest"})
      payload["mutation_history"] = [
          {k: v for k, v in entry.items() if k != "created_at"}
          for entry in payload.get("mutation_history", [])
      ]
      return payload
  ```
- `runtime_spec_digest()` calls `stable_hash("agintor.runtime_spec.v2", _spec_digest_payload(spec))`.
- Add a regression: `tests/test_runtime_spec_digest_stability.py` round-trips the baseline spec through JSON ten times and asserts the digest stays constant.

### 3.2 `OraclePackage` validation chokes on `BenchmarkTask.expected`

**Symptom:** Stack's `OracleTask.benchmark_task` is a `BenchmarkTask`, and `BenchmarkTask` requires `expected` for several `verifier_type`s. Stack's compiler builds `BenchmarkTask(... expected=None, private_expected=...)` which is fine, but Stack's QA runner re-projects through `runtime_visible_benchmark_task` which strips `private_expected` and then the validator sees neither `expected` nor `private_expected` and fails the QA's "answerable task" health check.

**Fix:**
- In `agintor/oracle/compiler.py`, set `expected={"oracle_claim_ids": [...]}` (the public oracle claim list) on the generated default tasks so the *runtime-visible* projection still has a non-empty `expected`. Move the actual ground-truth into `private_expected` exclusively.
- Add `verifier_type="oracle_package"` and `verification_required=True` so the existing host code knows to defer to the oracle runner instead of running the local JSON-exact verifier.
- Update `tests/test_oracle_public_projection.py` to assert that the public projection has `expected` populated and does not have `private_expected`.

### 3.3 `OracleCompiler` provider hook is dead code

**Symptom:** Stack's compiler accepts `provider: Any | None = None` but never calls it. The plan calls for an LLM-led compiler graph (§6 of the source plan).

**Fix scoped for pass 1:**
- Keep the deterministic path as the default. Add a single integration point: when `provider is not None`, call `compiler_graph.run(goal, runtime_spec, registry, provider) -> CompilerProposal` and use that proposal to **select** which families and claim packs the deterministic body emits. The provider proposes; deterministic code remains the only writer of the final `OraclePackage`. This matches §6.2 of the source plan ("specialist outputs are proposals").
- Wire `compiler_graph.run` to a stub that returns a proposal containing exactly the deterministic defaults when no provider is set — so the deterministic path is just `compile(provider=None)`.
- Defer real LangGraph wiring of the compiler graph to pass 2 — see §13.

### 3.4 Stack's loader diff has an indentation bug

**Symptom:** [`agintor_full_oracle_langgraph_stack/existing_edits/agintor__runtime__loader.py.search_replace.diff:22-37`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__runtime__loader.py.search_replace.diff:22) wraps the `for key, module_ref in manifest.policy_modules.items():` loop in an `else:` branch but only re-indents three lines of the loop body in the next block. The intermediate lines (the `module_path = …`, `import_module = …`, etc.) are left at the original indent and become unreachable / SyntaxError.

**Fix:** Re-author this edit by hand. The replacement should use a single SEARCH that matches the entire policy-modules block (including the body that hashes sources and counts AST nodes) and a REPLACE that wraps the whole block in `if runtime_kind in {…}:` / `else:`, indenting every line of the original body by one level inside the `else:` branch. Verify by `python -m compileall agintor/runtime/loader.py` before committing.

### 3.5 Patch's `public_oracle_projection` leaks sealed validator metadata

**Symptom:** [`agintor_full_plan_patch/new_files/agintor/oracle/projections.py:53-77`](agintor_full_plan_patch/new_files/agintor/oracle/projections.py:53) blanks `inputs/outputs_schema/health_tests` for sealed validators but leaves `validator_id`, `family_id`, `claim_ids`, `independence_group`, `authority_ceiling`, `failure_action` in the public payload.

**Fix:** Do not use Patch's `projections.py`. Stack's `oracle_public_projection` in [`contracts/oracle.py:320-376`](agintor_full_oracle_langgraph_stack/new_files/agintor/contracts/oracle.py:320) drops sealed validators entirely and only includes those with `visibility == "public"`. Re-export it from `agintor/oracle/projections.py`:
```python
from ..contracts.oracle import oracle_public_projection as public_oracle_projection
from ..contracts.oracle import oracle_sealed_projection as sealed_oracle_projection
```
Then the rest of the codebase can keep importing from `agintor.oracle.projections` per the plan's §13 module map.

### 3.6 `__pycache__` pollution

**Symptom:** Stack ships `__pycache__/` inside `new_files/`. Copying the bundle naively contaminates the repo with bytecode for Python 3.13 (the user is on 3.12).

**Fix:** When implementing §4 below, use a copy filter that excludes `__pycache__/` and `*.pyc`. A one-liner: `python -c "import shutil; shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))"`.

### 3.7 Stack's evaluator diff inserts a duplicate import block

**Symptom:** [`agintor_full_oracle_langgraph_stack/existing_edits/agintor__evaluation__evaluator.py.search_replace.diff:159-165`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__evaluation__evaluator.py.search_replace.diff:159) does a SEARCH on `from ..oracle.qa import OracleQARunner\nfrom ..oracle.package_io import load_oracle_package` and REPLACEs with the same lines plus `from .oracle_runner import OracleEvaluationRunner`. The earlier SEARCH/REPLACE in the same diff (lines 21-24) already added those two imports. Applying them in the diff order means the second SEARCH will fail to find a unique match.

**Fix:** Collapse the two import-related search/replace blocks into one SEARCH/REPLACE that targets the original `from ..utils import ensure_directory, stable_hash` line and inserts all three new imports at once. Apply this manually.

### 3.8 Stack's CLI diff assumes `baseline_langgraph_runtime_spec` exists in `agintor/contracts/__init__.py`

**Symptom:** Both bundles assume `default_langgraph_runtime_spec` (Patch) or `baseline_langgraph_runtime_spec` (Stack) is exported from `agintor.contracts`. Neither bundle defines this function.

**Fix:** Add the function to `agintor/contracts/runtime_spec.py`:
```python
def baseline_langgraph_runtime_spec(*, runtime_id: str, name: str = "Baseline LangGraph Runtime") -> RuntimeSpec:
    """Construct the canonical default RuntimeSpec for `init-runtime --runtime-kind langgraph_spec_v2`."""
    return RuntimeSpec(
        schema_version="agintor.runtime_spec.v2",
        runtime_id=runtime_id,
        runtime_kind="langgraph_spec_v2",
        name=name,
        description="Baseline spec-backed runtime; one direct-response agent terminating immediately.",
        agents=[AgentSpec(agent_id="agent.default", role="worker", prompt=PromptSpec(task_template="{prompt}"))],
        graph=GraphSpec(
            graph_id="runtime_graph",
            entry_node="node.default",
            terminal_nodes=["node.terminal"],
            nodes=[
                GraphNodeSpec(node_id="node.default", node_type="direct_response", agent_id="agent.default", output_key="answer"),
                GraphNodeSpec(node_id="node.terminal", node_type="verify", input_keys=["answer"]),
            ],
            edges=[GraphEdgeSpec(source="node.default", target="node.terminal")],
        ),
        tools=[],
        models=[ModelPolicy(model_policy_id="default", provider_name="runtime_default", model_class="small")],
        memory=MemoryPolicy(memory_policy_id="default", memory_kind="short_term"),
        execution=ExecutionPolicy(max_steps=32, side_effect_policy="receipt_required"),
        tracing=TracingPolicy(trace_level="full"),
        mutation_history=[],
        metadata={"template": "baseline_runtime_v2"},
    )
```
Export it from `agintor/contracts/__init__.py` alongside the other `runtime_spec` exports added in §4.1.

---

## 4. File-by-file action map

This is the canonical sequence. Follow it in order. Do not batch-apply with the bundle scripts.

### 4.1 Phase A — pure additions (no existing-file edits, no deps)

**Goal:** land the new contracts and helpers; keep `python -m pytest` green by NOT yet exporting them from `agintor.contracts.__init__`.

Copy these files from Stack into the live repo, filtering `__pycache__`/`*.pyc`:

| Destination | Source |
|---|---|
| `agintor/contracts/runtime_spec.py` | Stack `new_files/agintor/contracts/runtime_spec.py`, then apply §3.1 + §3.8 fixes |
| `agintor/contracts/spec_actions.py` | Stack |
| `agintor/contracts/oracle.py` | Stack |
| `agintor/oracle/__init__.py` | Stack |
| `agintor/oracle/package_io.py` | **Patch** (then rewire to Stack projection) |
| `agintor/oracle/projections.py` | New shim — see §3.5 |
| `agintor/oracle/qa.py` | Stack |
| `agintor/oracle/validator_registry.py` | Stack |
| `agintor/oracle/subagents.py` | Stack |
| `agintor/oracle/compiler.py` | Stack, then apply §3.2 + §3.3 fixes |
| `agintor/oracle/compiler_graph.py` | Stack |
| `agintor/oracle/families/__init__.py` | Stack |
| `agintor/oracle/families/exact_private_answer.py` | Stack |
| `agintor/oracle/families/schema_artifact.py` | Stack |
| `agintor/oracle/families/repo_patch.py` | Stack |
| `agintor/oracle/families/stateful_service.py` | Stack |
| `agintor/oracle/families/trace_state.py` | Stack |
| `agintor/oracle/families/factual_grounded.py` | Stack |
| `agintor/oracle/families/pairwise_preference.py` | Stack |
| `agintor/oracle/families/trading_outcome.py` | Stack |
| `agintor/oracle/families/human_audit.py` | Stack |
| `agintor/oracle/families/inspect_runner.py` | Stack |
| `agintor/oracle/families/openai_eval_runner.py` | Stack |
| `agintor/oracle/families/consent_proof.py` | Stack |
| `agintor/runtime/langgraph/__init__.py` | Stack |
| `agintor/runtime/langgraph/state.py` | Stack |
| `agintor/runtime/langgraph/operation_service.py` | Stack |
| `agintor/runtime/langgraph/checkpointing.py` | Stack |
| `agintor/runtime/langgraph/adapters.py` | Stack |
| `agintor/runtime/langgraph/compiler.py` | Stack, augmented with the LangGraph build path from Patch (see Action 4.4) |
| `agintor/runtime/langgraph/entrypoint.py` | Stack |
| `agintor/evaluation/oracle_runner.py` | Stack |
| `agintor/search/spec_mutator.py` | Stack |
| `agintor/integrations/tradingagents/__init__.py` | Stack |
| `agintor/integrations/tradingagents/spec.py` | Stack |
| `agintor/integrations/tradingagents/adapter.py` | Stack |
| `agintor/integrations/tradingagents/action_mapper.py` | Stack |
| `agintor/integrations/tradingagents/data_snapshots.py` | Stack |
| `agintor/integrations/tradingagents/ledgers.py` | Stack |
| `agintor/integrations/tradingagents/validators.py` | Stack |
| `agintor/integrations/tradingagents/outcome_oracle_family.py` | Stack |
| `agintor/integrations/tradingagents/compiler.py` | **Patch** (rewired to Stack profile) |
| `templates/baseline_runtime_v2/runtime_spec.json` | **Patch**, with `entry_node`/`terminal_nodes` rename |
| `templates/baseline_runtime_v2/langgraph_app.py` | **Patch** |
| `templates/baseline_runtime_v2/README.md` | Stack |

**Exit gate A:** `python -m compileall -q agintor templates`. No pytest changes yet.

### 4.2 Phase B — wire the new contracts into `agintor.contracts`

Apply this edit to `agintor/contracts/__init__.py`:

```python
# After:  from .runtime import *  # noqa: F401,F403
# Add:
from .runtime_spec import *  # noqa: F401,F403
from .spec_actions import *  # noqa: F401,F403

# After:  from .evidence import *  # noqa: F401,F403
# Add:
from .oracle import *  # noqa: F401,F403
```

Add to the `model_rebuild` block (after `PromotionDecision,`):

```python
    RuntimeSpec,
    SpecAction,
    OraclePackage,
```

(`OracleEvaluationSummary` exists in Patch's `oracle.py` but not Stack's — do not export it; Stack delivers the same payload through `OracleEvaluationRunner` results, which are not Pydantic models needing forward-ref rebuild.)

**Exit gate B:** `python -m compileall -q agintor` and `python -c "from agintor.contracts import RuntimeSpec, OraclePackage, SpecAction"`.

### 4.3 Phase C — thread oracle/runtime identity through evidence ledgers

This is Stack's [`agintor__contracts__evidence.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__contracts__evidence.py.search_replace.diff). Apply it. Anchors verified against the live repo: `EvidenceRecord`, `PairedComparison`, `ProgressSignal`, `PromotionDecision` all exist in `agintor/contracts/evidence.py` with the field shape the diff assumes.

Apply Stack's [`agintor__contracts__runtime.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__contracts__runtime.py.search_replace.diff) (`RuntimeManifest` extras: `runtime_kind`, `runtime_spec_path`, `runtime_spec_digest`, `oracle_package_hash`).

Apply Stack's [`agintor__contracts__search.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__contracts__search.py.search_replace.diff) (`oracle_package_hash` on archive/search records).

**Exit gate C:** `python -m pytest tests/test_evidence_ledger*.py` (whatever the existing repo names them) stays green. Existing fields are additive with defaults so no test should regress.

### 4.4 Phase D — runtime spec compilation and host loader

1. **`agintor/runtime/langgraph/compiler.py`** — final shape: Stack base + Patch's `StateGraph` build path. The class structure becomes:
   ```python
   class CompiledSpecRuntime:
       def __init__(self, runtime_spec, *, provider=None):
           self.runtime_spec = runtime_spec
           self.service = RuntimeOperationService(runtime_spec, provider=provider)
           self._lg_app = self._build_langgraph_app()  # None if optional dep missing
           self.backend = "langgraph" if self._lg_app else "sequential"

       def _build_langgraph_app(self):
           try:
               from langgraph.graph import StateGraph
           except Exception:
               return None
           graph = StateGraph(dict)
           for node in self.runtime_spec.graph.nodes:
               graph.add_node(node.node_id, self._lg_callable(node))
           graph.set_entry_point(self.runtime_spec.graph.entry_node)
           for edge in self.runtime_spec.graph.edges:
               graph.add_edge(edge.source, edge.target)
           for terminal in self.runtime_spec.graph.terminal_nodes:
               graph.set_finish_point(terminal)
           return graph.compile()

       def invoke(self, prompt, **kwargs):
           if self._lg_app is not None:
               return LangGraphRuntimeState(**dict(self._lg_app.invoke(initial_state(prompt, **kwargs))))
           return self._invoke_sequential(prompt, **kwargs)
   ```
   Both backends share `RuntimeOperationService`, so node behavior is identical regardless of which path runs.

2. Apply Stack's [`agintor__runtime__loader.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__runtime__loader.py.search_replace.diff) **after** rewriting it per §3.4. The corrected version targets:
   - Add `runtime_spec: Any | None = None` to `LoadedRuntime`.
   - Add the `from .langgraph.adapters import build_spec_policy_objects, load_runtime_spec` import.
   - Add the runtime-spec digest fingerprint to `immutable_fingerprints` (Stack's `RUNTIME_PROFILE_FILE` block edit).
   - Wrap the policy-modules loop body correctly inside `if runtime_kind in {"langgraph_spec_v2", "tradingagents_langgraph_v1"}: ... else: ...`. Verify with `python -m compileall agintor/runtime/loader.py` before committing.

**Exit gate D:**
- `python -m compileall -q agintor`.
- `python -c "from agintor.runtime.langgraph.compiler import CompiledSpecRuntime; from agintor.contracts import baseline_langgraph_runtime_spec; CompiledSpecRuntime(baseline_langgraph_runtime_spec(runtime_id='r1')).invoke('hi')"`.
- `python -m pytest tests/test_runtime_host.py tests/test_runtime_execution.py` (live repo tests must stay green).

### 4.5 Phase E — evaluator threads `OraclePackage` into ledgers + actually runs validators

Apply Stack's [`agintor__evaluation__evaluator.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__evaluation__evaluator.py.search_replace.diff) **after** consolidating the duplicate import block per §3.7.

Stack's diff does five load-bearing things; verify each landed:

1. `RuntimeEvaluator.__init__` accepts `oracle_package: OraclePackage | str | Path | None`.
2. `_oracle_identity_payload()` adds `oracle_package_hash`, `runtime_spec_digest`, `oracle_public_view_hash`, `oracle_sealed_view_hash` to evidence digests and ledger rows.
3. New `staged_evaluate_runtime_pair(parent_dir, child_dir, objective, mutation_action_ids=())` for spec-runtime pair evaluation.
4. New per-run call: `OracleEvaluationRunner.evaluate_run(self.oracle_package, run)` populates `validator_results` and `claim_results` on `EvidenceRecord`.
5. Pre-flight `OracleQARunner.run(self.oracle_package)` short-circuits Stage 4 with `oracle_package_qa: fail` if QA does not pass.

Apply Stack's [`agintor__evaluation__progress_oracle.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__evaluation__progress_oracle.py.search_replace.diff). This carries `oracle_package_hash`, `parent_runtime_spec_digest`, `child_runtime_spec_digest` into `ProgressSignal` and `PromotionDecision`. Add the comparison-blocking rule per source plan §10.3:

```python
# Inside ProgressOracle.decide(), before any axis comparison:
if comparison.oracle_package_hash and decision_input.parent_oracle_hash and decision_input.parent_oracle_hash != comparison.oracle_package_hash:
    return self._quarantine(comparison, reason_code="oracle_package_hash_mismatch")
```

(The exact field path will depend on how `ProgressOracle.decide` reads parent state in the live repo. Read the existing function before adding this; preserve its existing decision routing.)

**Exit gate E:**
- `python -m pytest tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py` (must stay green; new fields default to empty so existing pairs still compare).
- `python -m pytest tests/test_oracle_evaluation_runner.py` (new, ships with Stack — see §11).

### 4.6 Phase F — search engine + archive use the spec mutator and oracle hash

Apply Stack's [`agintor__search__engine.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__search__engine.py.search_replace.diff). Verify the `parent_loaded_runtime` branch:
```python
if str(getattr(parent_loaded_runtime.manifest, "runtime_kind", "policy_file_v1")) in {"langgraph_spec_v2", "tradingagents_langgraph_v1"}:
    spec_candidate = (self.provider_spec_mutator or self.spec_mutator).mutate(SpecMutationContext(...))
    stage_results = self.evaluator.staged_evaluate_runtime_pair(parent_dir, spec_candidate.child_runtime_dir, objective, mutation_action_ids=...)
```
This is the only place where v2 evolution branches off the v1 patch-mutator path.

Apply Stack's [`agintor__search__archive.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__search__archive.py.search_replace.diff) — adds `oracle_package_hash`, `runtime_spec_digest` to archive rows.

**Exit gate F:**
- `python -m pytest tests/test_search_*.py` (live repo). Existing v1 search must stay green.
- `python -m pytest tests/test_spec_mutator.py` (new, ships with Stack).

### 4.7 Phase G — factory export wires v2 and writes the oracle package

Apply Stack's [`agintor__factory__export.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__factory__export.py.search_replace.diff). After applying:

- `_write_seed_runtime` accepts `runtime_kind` and `goal_spec`.
- For `langgraph_spec_v2` / `tradingagents_langgraph_v1`, it calls `RuntimeSpecCompiler().compile_to_directory(runtime_spec, seed_runtime_dir, force=True)` and writes the oracle package via `write_oracle_package(package, seed_runtime_dir / "oracle")`.

**Verify the export does not include sealed material.** This is invariant 5 of §2. The exported runtime directory must contain `oracle/public.json` (from `oracle_public_projection`) but never `oracle/sealed.json`. The sealed projection is written to a separate evaluator-only path under `.agintor_runs/.../oracle_packages/`.

Patch's `package_io.py` already has the public/sealed split. After this phase, audit the exported runtime directory contents with the test added in §11 (`test_export_no_sealed_material`).

Apply Stack's [`agintor__factory__planning.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__factory__planning.py.search_replace.diff). This is small — just adds the `OracleCompiler` import.

**Exit gate G:**
- `agintor build-runtime "demo: respond with the prompt verbatim" --destination .tmp_v2_runtime --runtime-kind langgraph_spec_v2 --steps 0` produces a runtime directory.
- `python -m pytest tests/test_export_no_sealed_material.py` (new, see §11).

### 4.8 Phase H — CLI surfaces

Apply Stack's [`agintor__cli.py.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/agintor__cli.py.search_replace.diff). It adds:

- `init-runtime --runtime-kind` → routes through `RuntimeSpecCompiler.compile_to_directory()` for v2 kinds.
- `eval --oracle-package` → loads a frozen package via `load_oracle_package()` and passes it to the evaluator.
- `oracle-qa <package_dir>` → runs QA, exits non-zero on fail.
- `inspect-oracle <package_dir> [--public]` → prints sealed or public projection.

Add one CLI surface the Stack diff misses: `compile-oracle <goal> <destination>` (Patch had this; it is useful for hand-driving the compiler):

```python
@app.command("compile-oracle")
def compile_oracle_cmd(goal: str, destination: str, runtime_kind: str = typer.Option("langgraph_spec_v2", "--runtime-kind")) -> None:
    goal_spec = GoalSpec(goal_id=f"goal.{abs(hash(goal))}", raw_prompt=goal, normalized_goal=goal.strip())
    runtime_spec = baseline_langgraph_runtime_spec(runtime_id="runtime.preview")
    package = OracleCompiler().compile(goal_spec, runtime_spec)
    frozen = write_oracle_package(package, destination)
    typer.echo(json.dumps({"package_id": frozen.package_id, "package_hash": frozen.package_hash, "destination": destination}, indent=2, sort_keys=True))
```

**Exit gate H:**
- `agintor init-runtime .tmp_v2_dir --runtime-kind langgraph_spec_v2 --force` succeeds.
- `agintor compile-oracle "demo goal" .tmp_pkg --runtime-kind langgraph_spec_v2` produces `oracle/public.json` and `oracle/sealed.json` with matching `package_hash`.
- `agintor oracle-qa .tmp_pkg` exits 0.
- `agintor inspect-oracle .tmp_pkg --public` does not contain any of: `private_expected`, `sealed_inputs`, `sealed_fixture_refs`, `hidden_tests`, `promotion_thresholds`, `private_rubric`.

### 4.9 Phase I — optional dependencies

Apply Stack's [`pyproject.toml.search_replace.diff`](agintor_full_oracle_langgraph_stack/existing_edits/pyproject.toml.search_replace.diff). Adds `langgraph`, `inspect-ai` extras.

**Exit gate I:** `pip install -e ".[langgraph]"` succeeds; the test suite stays green both with and without the extra installed.

---

## 5. New module conventions

When editing the copied files in Phase A, enforce these:

- **No comments explaining what the code does.** The CLAUDE.md rule applies. Only keep comments that name a non-obvious invariant (e.g., "MutationActionRef.created_at is excluded from canonical payload — see synthesis plan §3.1").
- **All Pydantic models inherit from one of the existing base models** (`RuntimeSpecModel`, `OracleModel`). Do not introduce a third model base.
- **No `Literal["v1", "v2"]` schema_version unions** — single literal per model. Versioning of disposable artifacts is not in scope (CLAUDE.md).
- **`stable_hash` is the only hash function.** Do not introduce a parallel hashing helper.

---

## 6. Pyflakes + import hygiene

After Phase A, run `python -m pyflakes agintor/oracle agintor/runtime/langgraph agintor/integrations/tradingagents agintor/contracts/runtime_spec.py agintor/contracts/spec_actions.py agintor/contracts/oracle.py`. Both bundles import symbols they do not use (`EvidenceRef` in Patch's `oracle.py`, several `from typing import …` extras in Stack). Strip these.

---

## 7. Tests to land

These are the focused regressions for pass 1. They subsume both bundles' test files; do **not** copy the bundle test files wholesale because several reference functions that do not exist after the synthesis (`OracleEvaluationSummary`, Patch's `OracleTask` shape, etc.).

| Test file | What it must assert |
|---|---|
| `tests/test_runtime_spec.py` | `RuntimeSpec.model_validate(spec.model_dump())` is field-equal. Graph validators reject duplicate node IDs, missing entry, missing terminals. |
| `tests/test_runtime_spec_digest_stability.py` | Roundtripping the baseline spec through JSON 10× yields the same `runtime_spec_digest`. Mutating `mutation_history[*].created_at` does not change the digest. |
| `tests/test_spec_actions.py` | Each action type validates against a parent spec. Adding an agent updates the child digest. Action ledger lines are JSON-serializable. |
| `tests/test_oracle_package.py` | Constructing an `OraclePackage` with a hard claim that has no validator and no `unverifiable_reason` raises. `package_hash`, `public_view_hash`, `sealed_view_hash` are auto-derived and stable across roundtrips. |
| `tests/test_oracle_public_projection.py` | Public projection contains `expected` on each task and never contains keys: `private_expected`, `sealed_inputs`, `sealed_fixture_refs`, `hidden_tests`, `promotion_thresholds`, `private_rubric`. **Sealed validators are absent entirely** (regression for §3.5). |
| `tests/test_oracle_sealed_eval.py` | Evaluator path through `OracleEvaluationRunner` produces one `ValidatorResult` per `validator_spec` and one `ClaimResult` per claim. |
| `tests/test_oracle_qa.py` | `OracleQARunner` rejects packages that fail leakage scan, contain uncovered hard claims, or have `package_hash` not matching the recomputed value. |
| `tests/test_langgraph_runtime_compiler.py` | `RuntimeSpecCompiler.compile_to_directory(spec, dir)` writes `runtime_spec.json`, `runtime_manifest.json`, `deployment_contract.json`, the generated app file, and bundles the runtime kernel. `CompiledSpecRuntime(spec).invoke("hi")` returns a state with `status == "completed"`. When `langgraph` is installed, `CompiledSpecRuntime(spec).backend == "langgraph"`. |
| `tests/test_spec_mutator.py` | Heuristic mutator emits a `SpecActionMutationResult` with at least one `SpecAction`, a child runtime dir whose spec digest differs from the parent, and a mutation ledger row. |
| `tests/test_export_no_sealed_material.py` | After `agintor build-runtime ... --runtime-kind langgraph_spec_v2`, the destination directory has `oracle/public.json` and never `oracle/sealed.json`, never `private_expected` strings in any file. |
| `tests/test_tradingagents_adapter.py` | `TradingAgentsRuntimeSpec.compile_to_directory()` produces a runtime that loads. Outcome validator runs against a synthetic decision/order/fill ledger and emits a `pass`/`fail` `ValidatorResult`. |
| `tests/test_progress_oracle_oracle_hash_block.py` | `ProgressOracle` quarantines a parent/child pair with mismatched `oracle_package_hash`. |

**Test slice command** for fast iteration:

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_runtime_spec.py tests/test_runtime_spec_digest_stability.py tests/test_spec_actions.py tests/test_oracle_package.py tests/test_oracle_public_projection.py tests/test_oracle_qa.py tests/test_oracle_sealed_eval.py tests/test_langgraph_runtime_compiler.py tests/test_spec_mutator.py tests/test_progress_oracle_oracle_hash_block.py
```

Then the existing repo's load-bearing slices to confirm no regression:

```powershell
.\.venv\Scripts\python -m pytest tests/test_runtime_host.py tests/test_runtime_execution.py tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py tests/test_search_*.py
```

---

## 8. Apply order summary

1. Phase A — copy new files (filter `__pycache__`/`*.pyc`); apply §3.1, §3.2, §3.3, §3.5, §3.8 in-file.
2. Phase B — wire `agintor.contracts.__init__`.
3. Phase C — evidence ledger identity threading.
4. Phase D — runtime spec compiler + corrected loader (§3.4) + LangGraph build path.
5. Phase E — evaluator + progress oracle, with corrected import block (§3.7) and the comparison-block rule.
6. Phase F — search engine + archive.
7. Phase G — factory export.
8. Phase H — CLI.
9. Phase I — pyproject extras.
10. Land tests from §7 as you complete each phase, not in a single batch at the end.

After each phase, run `python -m compileall -q agintor` and the relevant test slice. Do not advance to the next phase until the prior one is green.

---

## 9. Verification checklist (corresponds to source plan §16)

Tick each only after the corresponding test in §7 passes:

- [ ] New runtimes can be represented as `RuntimeSpec` (test_runtime_spec, test_runtime_spec_digest_stability).
- [ ] New runtimes compile into a runnable LangGraph/LangChain app via `RuntimeSpecCompiler.compile_to_directory` (test_langgraph_runtime_compiler).
- [ ] Runtime mutation happens through typed `SpecAction`s and writes a mutation ledger (test_spec_mutator).
- [ ] A frozen `OraclePackage` is created from `GoalSpec` (test_oracle_package).
- [ ] The package has public and sealed projections (test_oracle_public_projection, test_oracle_sealed_eval).
- [ ] The evaluator gives candidates only the public projection (test_export_no_sealed_material + manual `inspect-oracle --public`).
- [ ] Evidence records cite `package_hash`, `contract_id`, validator IDs, runtime spec digest (Phase E acceptance — inspect a `.agintor_runs` row).
- [ ] `ProgressOracle` remains the promotion gate (test_progress_oracle still green, no LLM calls inside `progress_oracle.py`).
- [ ] Search/archive updates require an evidence digest and decision type (Phase F edits include this — verify the archive row schema).
- [ ] TradingAgents registers as a default runtime/validator family without hardcoding finance into the generic compiler (test_tradingagents_adapter).

---

## 10. CLI surfaces after pass 1

```bash
agintor init-runtime <dir> --runtime-kind langgraph_spec_v2
agintor build-runtime "<goal>" --destination <dir> --runtime-kind langgraph_spec_v2 --steps 10
agintor solve <runtime_dir> --prompt "..."   # works on v1 and v2 runtimes
agintor eval <runtime_dir> --suite demo --oracle-package <pkg_dir>
agintor evolve <runtime_dir> --steps 10 --mutator heuristic-spec
agintor compile-oracle "<goal>" <pkg_dir> --runtime-kind langgraph_spec_v2
agintor oracle-qa <pkg_dir>
agintor inspect-oracle <pkg_dir> [--public]
```

`inspect-runtime` and `diff-runtime` from source plan §13.2 are out of scope for pass 1.

---

## 11. Out-of-scope deferrals

These items appear in the source plan but are **not** part of pass 1. Add them to `Dev Docs/DEFERRED_ISSUES_LEDGER.md` as separate items per CLAUDE.md.

- LLM-led `compiler_graph` real wiring — only the deterministic-via-proposal hook lands now (§3.3).
- `OracleCompiler` adaptive subagent loop with goal/domain/benchmark/validator authors as live LangGraph nodes.
- `inspect-runtime` / `diff-runtime` / `diff-oracle` CLIs.
- Mermaid export.
- LangSmith tracing wiring.
- WS3 changes — embedding LangGraph state into `CheckpointEnvelope` requires a separate WS3 plan; until then `CompiledSpecRuntime.invoke` is non-resumable.
- Inspect-AI runner, OpenAI Evals runner — the family files exist with stub `_run` bodies; live integration is pass 2.

---

## 12. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Stack's `OracleCompiler` hardcodes domain trigger words ("trading", "repo", etc.). New domains require editing the compiler core. | High | §3.3 keeps the deterministic body but moves trigger detection behind the registry's `family.applicability(context)`. Before adding a new domain, register a family with applicability and remove the keyword check from the compiler. |
| LangGraph 0.2 → 0.3 API drift. | Medium | `_build_langgraph_app` is the single import site. Wrap in `try/except` and treat any import error as "fall back to sequential". Add a smoke test in CI that imports `langgraph.graph.StateGraph` and skips with a clear message if missing. |
| The four-layer architecture boundary erodes if `OracleCompiler` calls into `task_runtime/*`. | Medium | Audit imports in `agintor/oracle/*` after Phase A. The only allowed imports from the runtime side are `agintor.contracts.*`, `agintor.utils`, and `agintor.runtime.langgraph.compiler` (factory-side wrapper). Add a unit test that walks `agintor/oracle/**.py` and asserts no import path matches `agintor.task_runtime` or `agintor.runtime_sdk`. |
| Comparison-block rule (§4.5) breaks existing v1 promotion flow because old comparisons have empty `oracle_package_hash`. | Medium | The block fires only when **both** sides have a non-empty hash and they differ. v1→v1 comparisons have empty hashes on both sides and pass through. Cover this with two test cases in `test_progress_oracle_oracle_hash_block.py`. |
| Stack's existing-edit anchors drift if the live repo evolves before this lands. | Medium | Phase C/E/F/G/H land within the same workstream sprint. If anchors drift, re-derive the SEARCH block from the current file content; do not blindly retry the diff. |

---

## 13. Glossary

- **Stack** — the bundle at `agintor_full_oracle_langgraph_stack/`.
- **Patch** — the bundle at `agintor_full_plan_patch/`.
- **`RuntimeSpec`** — the v2 typed runtime genome. JSON-canonicalized, hashable, mutated through `SpecAction`s.
- **`OraclePackage`** — frozen validation artifact for one `GoalSpec` + `RuntimeSpec`. Has public and sealed projections.
- **`OracleEvaluationRunner`** — runs each `ValidatorSpec` against a `RunResult`, produces `ValidatorResult`s and `ClaimResult`s.
- **`SpecActionMutator`** — replaces `HeuristicPatchMutator` for v2 runtimes. Emits typed `SpecAction`s; does not edit Python policy files.
- **Pass 1** — the slice that ends at the §9 checklist. Pass 2 covers the deferrals in §11.

---

## 14. Reference index

- Source design plan: [`LangGraph and Oracle Refactor Plan pass 1.md`](LangGraph and Oracle Refactor Plan pass 1.md)
- Stack bundle: [`agintor_full_oracle_langgraph_stack/`](agintor_full_oracle_langgraph_stack/)
- Patch bundle: [`agintor_full_plan_patch/`](agintor_full_plan_patch/)
- Architectural rules: [`CLAUDE.md`](CLAUDE.md)
- Workstream scope: `implementation_workstreams/WS4_*.md` (read this before starting Phase G).
- Deferred issue tracker: `Dev Docs/DEFERRED_ISSUES_LEDGER.md` (update after each phase if you find scope outside WS4).
