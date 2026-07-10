# LangGraph + Oracle Refactor — Pass 1 Implemented Reference

Status: implemented reference. This document records the plan that produced the LangGraph runtime and adaptive `OracleCompiler` pass-1 work. It is not a current implementation handoff or task list. It superseded:

- `Dev Docs/Archive Only - Zero Authority/LANGGRAPH_ORACLE_SYNTHESIS_PLAN.md` — retained as historical reference.
- `Dev Docs/Archive Only - Zero Authority/ORACLE_LANGGRAPH_REFACTOR_IMPLEMENTATION_PLAN.md` — retained as historical reference.
- The two raw patch bundles `agintor_full_plan_patch/` and `agintor_full_oracle_langgraph_stack/` — removed after pass-1 synthesis; their contents are not live code.

The source design plan is retained at `Dev Docs/Archive Only - Zero Authority/LangGraph and Oracle Refactor Plan pass 1.md`. Current implementation authority comes from live code, tests, `AGENTS.md`, and current retained documentation.

Do not resume the phases below as current work. Verify any historical claim against the live repository.

---

## 0. Historical implementation summary

- **Base bundle:** `agintor_full_oracle_langgraph_stack/` ("Stack"). More complete and integrated, but does not apply cleanly and has real defects (§7).
- **Reference bundle:** `agintor_full_plan_patch/` ("Patch"). Cleaner skeletons, narrower edits, missing the runner half. Lift specific named items; ignore the rest.
- **Posture:** treat both bundles as raw material. Do not run either apply script blindly. Every existing-file edit is re-verified against the live repo before it lands.
- **Bundle path note:** raw bundle paths below are historical labels only. Both bundles were removed and are recoverable only from Git history before `6226799`.
- **Historical Definition-of-Done evidence:** §14. Its checkbox state records the original implementation slice, not the current backlog.
- **Historical workstream label:** WS4. Items historically classified as WS3 (state and durability) were deferred in this plan — see §16.
- **Historical apply order:** §12 records how the implementation was landed.

---

## 1. Historical bundle-source decision (per file)

This table records the per-file bundle source used during synthesis. It is not a current source-of-truth map.

| Layer | Winner | Reason | Action |
|---|---|---|---|
| `agintor/contracts/runtime_spec.py` | Stack | Richer node types (`direct_response`, `builtin`, `service_action`, `repo_patch`), `PromptSpec`, `scope` per agent, structured graph validation. | Copy Stack file. Then apply digest-stability fix (§7.1) and add `baseline_langgraph_runtime_spec()` helper (§7.8). Add the private/sealed key/path scanner per §4 — replace any broad substring scan with a recursive key/path-based check. |
| `agintor/contracts/spec_actions.py` | Stack | Has the typed action-type enum the plan calls for and the validation hooks the search engine needs. | Copy Stack file. Verify `action_id` stability across roundtrips (§7.1). Apply the same private/sealed key/path scanner (§4) to action `patch` payloads. |
| `agintor/contracts/oracle.py` | Stack | `OracleTask` wraps the existing `BenchmarkTask` so the host already knows how to project it. Auto-derives `package_hash`/`public_view_hash`/`sealed_view_hash` in `model_validator`. Refuses to construct a package with uncovered hard claims. | Copy Stack file. Then apply the `BenchmarkTask.expected` integration fix (§7.2). |
| `agintor/oracle/projections.py` | Stack-pattern | Stack's projection lives in `contracts/oracle.py` (`oracle_public_projection`); Patch's standalone module leaks sealed validator metadata (`validator_id`, `family_id`, `claim_ids`, `independence_group`). | Use Stack's projection logic. Re-export it from `agintor/oracle/projections.py` so call sites have a stable import path. Drop Patch's `_strip_private` walker (§7.5). |
| `agintor/oracle/qa.py` | Stack | Larger and validates leakage by re-running projections + recomputing hashes. | Copy Stack file. |
| `agintor/oracle/package_io.py` | Patch | Stack writes only the sealed package; Patch writes both projections + a manifest. | Copy Patch file. Adjust to call Stack's `oracle_public_projection` per §7.5. |
| `agintor/oracle/validator_registry.py` | Stack | Registry exposes `family.run_validator(spec, payload)` and `family.make_spec(...)`. Patch's `build_specs`/`can_handle` model is a declaration-only API that leaves validators inert. | Copy Stack file. Enforce the full `ValidatorFamily` interface per §3. |
| `agintor/oracle/families/*.py` | Stack | Every family in Stack has a `_run(spec, payload) -> ValidatorResult` body. Patch's families only emit specs. | Copy all 11 Stack family files including `consent_proof.py`. Discard Patch's family bodies entirely. Apply the error-semantics rule per §3 (exceptions become `ValidatorResult(status="error")`, not silent drops). |
| `agintor/oracle/compiler.py` | Stack | Patch compiler accepts `task_sets=()` and emits packages with **zero tasks**, which trips QA. Stack compiler emits real default tasks with claim coverage and routes claims to families per detected domain. | Copy Stack file. Then apply provider-hook fix (§7.3) and gate domain-specific claim packs behind `family.score_applicability(context)` per §17 risk-register row 1, so adding a new domain does not require editing the compiler core. |
| `agintor/oracle/compiler_graph.py` | Stack | Larger LangGraph workflow shell with proper subagent dispatch placeholders. | Copy Stack file. Live wiring is pass 2 (§16). |
| `agintor/oracle/subagents.py` | Stack | Larger, names the subagents the source plan §6.2 lists. | Copy Stack file. |
| `agintor/evaluation/oracle_runner.py` | Stack-only | **Patch does not have this file.** Without it, validators never execute and `ClaimResult`s are never produced. | Copy Stack file. This is the single biggest reason Patch is non-viable. |
| `agintor/runtime/langgraph/state.py` | Stack | Pydantic state model, not a dict, so the LangGraph adapter and the sequential walker share types. | Copy Stack file. Solve-time module (§6). |
| `agintor/runtime/langgraph/operation_service.py` | Stack | Real dispatcher: agent / builtin / tool / merge / verify / service_action / repo_patch. Records side-effect receipts with idempotency keys and action fingerprints. Patch has a thin Protocol shim that does nothing. | Copy Stack file. Solve-time module (§6). Enforce the full node-type dispatch list per §5: `direct_response`, `agent`, `builtin`, `tool`, `merge`, `verify`, `service_action`, `repo_patch`. Side-effect nodes must produce receipts with idempotency keys and action fingerprints. Tool nodes may only call runtime-visible tools. |
| `agintor/runtime/langgraph/compiler.py` + solve-time executor module | **Hybrid** | Stack's `RuntimeSpecCompiler.compile_to_directory` correctly writes spec + generated app + `RuntimeManifest` spec-backed + `DeploymentContract` and bundles the runtime kernel. **But Stack's `CompiledSpecRuntime` is a hand-rolled sequential walker that never constructs a `langgraph.graph.StateGraph`** — the plan explicitly requires LangGraph as the substrate. Patch has the lazy-import + `StateGraph` build path that Stack lacks. | Start from Stack's file, but split factory/export code from solve-time execution. `RuntimeSpecCompiler.compile_to_directory`, bundling, generated app writing, and oracle-package writing stay factory-side. `CompiledSpecRuntime`, `compile_runtime_spec`, backend selection, constants imported by adapters/generated apps, and `RuntimeOperationService` wiring move to a solve-time module such as `executor.py` (or an equivalently isolated module). Add Patch's lazy-import pattern there: `try: from langgraph.graph import StateGraph` → build the real graph → fall through to the sequential walker only when the optional dep is missing. Surface the chosen backend on `CompiledSpecRuntime.backend`. Use only the LangGraph API subset documented in §13.7. |
| `agintor/runtime/langgraph/adapters.py` | Stack | Has `load_runtime_spec` and `build_spec_policy_objects` referenced by the loader edit. | Copy Stack file. Solve-time module (§6). Verify both symbols exist before the loader diff lands. |
| `agintor/runtime/langgraph/checkpointing.py` | Stack | Both are thin; Stack has the right shape for embedding into `CheckpointEnvelope` (per source plan §2 and §13.2 of this plan). | Copy Stack file. Solve-time module (§6). Pass 1: stub only; real embedding is WS3 work (§16). |
| `agintor/runtime/langgraph/entrypoint.py` | Stack-only | Bridges the spec runtime back into the existing host protocol. Patch lacks this; without it the spec-backed runtime cannot be invoked through `agintor solve`. | Copy Stack file. Solve-time module (§6). Wire it from the generated runtime manifest's `policy_modules` dict. |
| `agintor/search/spec_mutator.py` | Stack-shape | Both implement `SpecActionMutator`. Stack's heuristic + provider variants line up with the search engine edit. Patch's variant is named `SpecMutationContext` only and does not pair with an evaluator integration. | Copy Stack file. |
| `agintor/integrations/tradingagents/*` | **Hybrid** | Stack's `adapter.py`, `data_snapshots.py`, `ledgers.py`, `action_mapper.py` are larger and integrate with the validator runner. Patch's `compiler.py` is more substantial and implements the trading runtime spec compilation Stack handles in 4 lines. The historical implementation used an optional local TradingAgents checkout at upstream commit `a5cb7cb`; that checkout is not tracked or required. | Take Stack files for everything *except* `compiler.py`; for `compiler.py`, take Patch's implementation and rewire it to consume Stack's `TradingAgentsRuntimeSpec` profile. The trading validator family must cover the evidence list in §18. |
| `agintor/oracle/families/trading_outcome.py` | Stack | Has runner. | Copy Stack file. Evidence list per §18. |
| `agintor/templates/baseline_runtime_langgraph/runtime_spec.json` | Patch | Patch ships a concrete `runtime_spec.json`. Stack only ships a README and depends on `baseline_langgraph_runtime_spec()` to materialize the template at runtime. | Ship the Patch JSON. **Reconcile field names with Stack contracts first** — Stack uses `entry_node` and `terminal_nodes`; Patch's JSON uses `entry_node_id` and `terminal_node_ids`. Use Stack's names since the contracts are Stack's. **Place under `agintor/templates/baseline_runtime_langgraph/`, not repo-root `templates/`.** |
| `agintor/templates/baseline_runtime_langgraph/langgraph_app.py` | Patch | Concrete reference implementation. | Same placement note as above. |
| `agintor/templates/baseline_runtime_langgraph/README.md` | Stack | | Same placement note. |

### Things to *not* take from either bundle

- `agintor_full_plan_patch/write_patch_files*.py` — these are emission scripts the bundle generator used internally, not artifacts.
- `agintor_full_oracle_langgraph_stack/__pycache__/` and all `agintor_full_*/new_files/**/__pycache__/`, `*.pyc` — pre-compiled bytecode pollution. Filter these out when copying (§7.6).
- `agintor_full_plan_patch/EXISTING_FILE_EDITS.search_replace.md` — Patch's existing-edit map is shallower than Stack's and does not close the loop on factory/search/CLI. Use Stack's individual `existing_edits/*.diff` files as the **starting point** for §8 below, but verify every anchor by hand.
- `agintor_full_oracle_langgraph_stack/apply_search_replace.py` — bundle apply script. Several diffs are stale or contain bugs (§7.4, §7.7). Apply edits manually.
- **Repo-root `templates/baseline_runtime_langgraph/`** — Patch's bundle places templates here. The Agintor convention places runtime templates under `agintor/templates/`. Move them.
- **Sealed oracle package files into exported runtime directories** — see invariant §2.5.
- Patch's `agintor/oracle/projections.py` (`_strip_private` walker) — leaks sealed validator metadata (§7.5).
- Stack's `apply_search_replace.py`'s wholesale apply of `agintor__runtime__loader.py.search_replace.diff` — has an indentation bug (§7.4).
- Patch's `agintor/oracle/family/*.py` `build_specs`-only bodies — declaration-only, never run validators.

---

## 2. Historical pass-1 architecture invariants

These constraints governed pass 1. Current changes must follow `AGENTS.md`, live code, and current tests.

1. **Factory / host / runtime-kernel / policy boundary stays separated.** Factory, evaluation, and search code under `agintor/factory/`, `agintor/evaluation/`, and `agintor/search/` calls the runtime through `RuntimeHost`, `agintor/runtime/api/`, or the protocol entrypoint, never through `agintor/runtime/kernel/` internals. Bundled solve-time code lives under `agintor/runtime/sdk/`, `agintor/runtime/kernel/`, and `agintor/runtime/langgraph/`. Current enforcement lives in `tests/test_import_boundaries.py`.
2. **Single `RUNTIME_CONTRACT_VERSION`.** No new ABI/storage axes, no extra schema marker fields, and no numbered runtime/schema names. No legacy migration code for existing checkpoints, traces, or exported runtimes — they are disposable per `AGENTS.md`. Runtime kinds are plain strings under the existing runtime contract (§13.1).
3. **One `ExecutionPlan`.** Both prompt solves and benchmark solves still compile to one plan. The `langgraph_spec` path adds a second runtime *kind*, not a second plan format.
4. **`ProgressOracle` stays the promotion authority.** The `OracleCompiler` produces frozen packages; it does not decide promotion. Validator runners produce evidence; they do not decide promotion.
5. **Exported runtime never carries sealed oracle material.** The sealed `OraclePackage` projection, `oracle_sealed_view_hash` artifacts, private fixtures, hidden tests, promotion thresholds, private rubrics, and oracle compiler traces that reveal private authority are evaluator-only. Verified by `tests/test_export_no_sealed_material.py` and `agintor inspect-oracle --public`.
6. **Pairwise comparisons require matching `oracle_package_hash`.** A child evaluated under a new package versus a parent evaluated under an old package is a `quarantine`, not a promotion. policy-module↔spec-backed boundary case explicit (§13.4).
7. **TradingAgents is one runtime profile and one validator family.** Pass 1 used an optional local checkout at upstream commit `a5cb7cb`; current authority is `agintor/integrations/tradingagents/`. TradingAgents is not the root oracle and never defines validation for non-trading goals.
8. **Optional deps stay optional.** `langgraph`, `langchain`, `langsmith`, `inspect-ai` are added under `pyproject.toml` `[project.optional-dependencies]` (Stack's `pyproject` diff is correct). The deterministic sequential fallback path must keep `python -m pytest` green when those extras are not installed.
9. **OracleCompiler is invoked once per `GoalSpec`.** The resulting frozen `OraclePackage` is cached and shared across the entire evolution loop for that goal. New package only on goal amendment or `signal_sufficiency` gap (§13.3).
10. **LangGraph API subset is fixed for pass 1.** Only `StateGraph(state_schema)`, `add_node`, `set_entry_point`, `add_edge`, `set_finish_point`, `compile`, `invoke`. No conditional edges, no parallel branches, no interrupt/resume (§13.7). Documented in the solve-time executor module docstring.

---

## 3. Validator family contract

The validator family registry must enforce a single interface. Stack's `_run` shape is correct in spirit; this section makes it formal.

```python
from typing import Any, Literal, Protocol


class ValidatorFamily(Protocol):
    family_id: str
    authority_ceiling: Literal["A0", "A1", "A2", "A3", "A4", "A5"]
    default_visibility: Literal["public", "private", "sealed"]
    default_failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"]
    input_contract: dict[str, Any]
    output_schema: dict[str, Any]
    leakage_risks: list[str]
    health_tests: list[dict[str, Any]]

    def score_applicability(self, context: dict[str, Any]) -> float: ...
    def make_spec(self, *, claim_ids: list[str], inputs: dict[str, Any], visibility: str, **kwargs: Any) -> "ValidatorSpec": ...
    def run_validator(self, spec: "ValidatorSpec", payload: dict[str, Any]) -> "ValidatorResult": ...
```

### Family roster (pass 1)

1. `exact_private_answer` — high-authority canary, not general oracle.
2. `schema_artifact` — JSON/file/artifact schema and behavioral checks.
3. `repo_patch` — patch apply, public tests, hidden tests, tamper evidence (SWE-bench style).
4. `stateful_service` — expected final state, duplicate/forbidden side effects, API-state validation (tau-bench style).
5. `trace_state` — required/forbidden trace events, budgets, receipts (LangGraph/AgentEvals style).
6. `pairwise_preference` — weak authority unless calibrated and paired with stronger checks.
7. `factual_grounded` — citation/freshness/contradiction checks, authority capped below sealed tests.
8. `trading_outcome` — cutoff, order validity, fill reconciliation, portfolio, cost/risk, post-close snapshots (§18).
9. `consent_proof` — side-effect consent and receipt validation.
10. `human_audit` — optional high-authority human-signed result.

Optional families (stub `_run` bodies in pass 1, live integration is pass 2 per §16):

- `inspect_runner` — adapter to Inspect AI tasks/scorers/solvers/sandboxes.
- `openai_eval_runner` — adapter to OpenAI Evals.

### Error semantics

- Validator execution exceptions must produce `ValidatorResult(status="error")`; do not silently drop them.
- Weak model/human preference validators cannot promote alone — their authority ceiling is enforced by `OracleCompiler` claim routing.
- Each family ships one positive control and one negative control test.

---

## 4. Private/sealed key/path scanner spec

Both `RuntimeSpec` validation and `SpecAction.patch` validation must reject private/sealed material before the spec is digested or the action is applied. The scanner must use recursive key/path checks, **not** raw substring matching over all values — legitimate strings like a runtime ID or description containing the word "sealed" must pass.

### Forbidden key prefixes (recursive)

- `private_`
- `sealed_`
- `hidden_`
- `oracle_private_`

### Forbidden exact key names (recursive)

- `private_expected`
- `private_answer`
- `private_answer_ref`
- `hidden_tests`
- `private_rubric`
- `promotion_threshold`
- `sealed_inputs`
- `sealed_fixture_refs`

### Forbidden tool entries

- Any `ToolSpec` with `authority_boundary == "sealed_validator"` must be rejected from runtime-visible tool lists.

### Allowed

- Ordinary values containing words like `"sealed"`, `"private"`, `"hidden"` inside names, IDs, descriptions, or prompts. Example: `runtime_id="sealed-room-finder"` is allowed; the rejection is on key name, not value text.

### Tests (folded into §11)

- `tests/test_runtime_spec.py::test_forbidden_keys_rejected` — covers each prefix and each exact name.
- `tests/test_runtime_spec.py::test_legitimate_strings_allowed` — covers ID and description containing the words.
- `tests/test_spec_actions.py::test_patch_with_forbidden_key_rejected` — the same scanner applied to action patch payloads.

---

## 5. RuntimeOperationService dispatch

`RuntimeOperationService` is the solve-time service that LangGraph nodes call into. It must dispatch the following node types:

- `direct_response` — produce the final answer directly from the agent prompt.
- `agent` — invoke an agent loop (LangChain `create_agent` when LangChain installed, deterministic fallback otherwise).
- `builtin` — invoke a built-in deterministic helper (echo, summarize, format).
- `tool` — invoke a runtime-visible tool from the spec's `tools` list.
- `merge` — combine multiple input keys into one output key.
- `verify` — run a runtime-visible verifier (NOT a sealed validator).
- `service_action` — perform a side-effecting service call (HTTP, repo, DB).
- `repo_patch` — apply a repository patch to the workspace.

### Side-effect node requirements

- Side-effect nodes (`service_action`, `repo_patch`) must produce receipts with:
  - `idempotency_key` (deterministic from action + inputs).
  - `action_fingerprint` (hash of the action payload).
  - `outcome_status` (`applied`, `noop`, `rejected`).
- Receipts feed into the existing host-side side-effect ledger and into the `trace_state` validator family.

### Tool restriction

- Tool nodes may only call tools listed in `runtime_spec.tools`. The `OracleCompiler` must not list any tool with `authority_boundary == "sealed_validator"` in the public projection.

### Host-gated side effects

- For any side effect the host must execute (e.g., production HTTP calls), the runtime records *intent* and the host performs and returns a receipt. The runtime never executes the host-gated side effect directly.

---

## 6. Solve-time vs factory-time split inside `runtime/langgraph/`

The generated runtime app and the bundled runtime kernel must not import factory/search/evaluation code. Split the package by import surface:

### Solve-time modules (bundled into the runtime kernel and shipped with exported runtimes)

- `agintor/runtime/langgraph/state.py`
- `agintor/runtime/langgraph/operation_service.py`
- `agintor/runtime/langgraph/adapters.py`
- `agintor/runtime/langgraph/entrypoint.py`
- `agintor/runtime/langgraph/checkpointing.py`
- `agintor/runtime/langgraph/executor.py` (or an equivalently isolated solve-time module)

Allowed imports for solve-time modules: `agintor.contracts.*`, `agintor.utils`, `agintor.providers`, `agintor.runtime.api.*`, `agintor.runtime.kernel.*` (read), `agintor.runtime.tools.*`. **Not allowed**: `agintor.search.*`, `agintor.evaluation.*`, `agintor.factory.*`, `agintor.oracle.*`.

### Factory/export-time module (factory side only, never bundled)

- `agintor/runtime/langgraph/compiler.py`

This module may call `bundle_runtime_kernel(...)`, `RuntimeSpecCompiler.compile_to_directory(...)`, and `write_oracle_package(...)`.

`adapters.py`, generated runtime app code, and runtime host paths must not import the factory/export compiler. If the Stack or Patch code imports `.compiler` from solve-time modules, move the solve-time symbols (`CompiledSpecRuntime`, `compile_runtime_spec`, backend selection, runtime-spec file constants) into `executor.py` and update those imports. The factory compiler may import the solve-time executor to smoke-test generated output; solve-time code may not import the factory compiler.

### Host/runtime entry integration

Add a spec-backed fast path in the existing runtime entry flow. The narrowest acceptable sites are `agintor/runtime/sdk/entrypoint.py` after `load_runtime(...)` has produced `LoadedRuntime.runtime_spec`, or `TaskRuntime.run_task()` before the old topology/memory/tool/control kernel compiles an `ExecutionPlan`. That fast path delegates to the solve-time executor and returns through the existing runtime response protocol.

`SpecBackedPolicy` and `build_spec_policy_objects(...)` are loader compatibility objects only. They are not the main execution bridge, and they must not hide a second run through the old policy-module kernel.

### Kernel bundle inclusion

Add to `agintor/runtime/sdk/bundle.py` the new solve-time modules listed above, plus `agintor/contracts/runtime_spec.py` (because the entrypoint needs to deserialize `RuntimeSpec` at solve time). The bundled contract shim must export `RuntimeSpec` and the related model classes. Do **not** bundle `agintor/contracts/oracle.py` — sealed oracle material must stay factory/evaluator-side.

### Boundary test

Add `tests/test_runtime_langgraph_solve_time_imports.py`:

```python
import ast
from pathlib import Path

FORBIDDEN_PREFIXES = ("agintor.search", "agintor.evaluation", "agintor.factory", "agintor.oracle")
SOLVE_TIME_FILES = [
    "agintor/runtime/langgraph/state.py",
    "agintor/runtime/langgraph/operation_service.py",
    "agintor/runtime/langgraph/adapters.py",
    "agintor/runtime/langgraph/entrypoint.py",
    "agintor/runtime/langgraph/checkpointing.py",
    "agintor/runtime/langgraph/executor.py",
]

def test_solve_time_modules_have_no_factory_imports():
    for path in SOLVE_TIME_FILES:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in getattr(node, "names", [])]
                module = getattr(node, "module", None)
                candidates = ([module] if module else []) + names
                for candidate in candidates:
                    if any(str(candidate).startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
                        raise AssertionError(f"{path} imports forbidden {candidate}")
```

---

## 7. Defects in the bundles to fix during synthesis

Both bundles ship with defects the other agents reported and that source inspection confirms. Each item below is a concrete fix you must apply on top of the copy operations in §1.

### 7.1 `RuntimeSpec` digest instability

**Symptom:** `runtime_spec_digest(spec)` returns a different value on `model_validate(json.dumps(spec))` roundtrip.

**Root cause:** Both bundles include `created_at: float = Field(default_factory=now_ts)` on `MutationActionRef`. When a spec is dumped and reloaded with no `created_at`, `default_factory` re-fires and the digest changes.

**Fix:**

- Make `created_at` excluded from canonical-payload calculation. Define a private helper:
  ```python
  _DIGEST_EXCLUDE = {"created_at", "spec_digest", "parent_spec_digest", "metadata"}

  def _spec_digest_payload(spec: "RuntimeSpec") -> dict[str, Any]:
      payload = spec.model_dump(mode="json", exclude_none=True, exclude={"spec_digest"})
      payload["mutation_history"] = [
          {k: v for k, v in entry.items() if k != "created_at"}
          for entry in payload.get("mutation_history", [])
      ]
      return payload
  ```
- `runtime_spec_digest()` calls `stable_hash("agintor.runtime_spec", _spec_digest_payload(spec))`.
- The same exclusion strategy applies to `OraclePackage.public_view_hash`/`sealed_view_hash`/`package_hash` — strip any incidental timestamps (`created_at`, `qa_report.completed_at`) before hashing.
- Add a regression: `tests/test_runtime_spec_digest_stability.py` round-trips the baseline spec through JSON ten times and asserts the digest stays constant.
- Decide explicitly: `metadata` does not participate in identity unless a specific metadata field is load-bearing.

### 7.2 `OraclePackage` validation chokes on `BenchmarkTask.expected`

**Symptom:** Stack's `OracleTask.benchmark_task` is a `BenchmarkTask`, and `BenchmarkTask` requires `expected` for several `verifier_type`s. Stack's compiler builds `BenchmarkTask(... expected=None, private_expected=...)` which is fine, but Stack's QA runner re-projects through `runtime_visible_benchmark_task` which strips `private_expected` and then the validator sees neither `expected` nor `private_expected` and fails the QA's "answerable task" health check.

**Fix:**

- Do not put sealed ground truth in runtime-visible `expected`. Runtime-visible data may contain only non-secret oracle claim references such as `{"oracle_claim_ids": [...]}`.
- The live `runtime_visible_benchmark_task(...)` helper strips `expected` whenever `private_expected` exists. Fix the plan implementation by choosing one explicit shape and testing it:
  - Preferred: keep sealed answer material inside the sealed `OraclePackage` payload, not in `BenchmarkTask.private_expected`, and keep public claim refs in `expected`.
  - Acceptable alternative: add a narrow `verifier_type="oracle_package"` projection exception that preserves only `expected={"oracle_claim_ids": [...]}` while still stripping `private_expected` and all answer material.
- Add `verifier_type="oracle_package"` and `verification_required=True` so the existing host code knows to defer to the oracle runner instead of running the local JSON-exact verifier. Dispatch site detailed in §13.5.
- Update `tests/test_oracle_public_projection.py` to assert that the public projection has no sealed answer material. If `expected` is present, it contains only `oracle_claim_ids`; it never contains `private_expected`.

### 7.3 `OracleCompiler` provider hook is dead code

**Symptom:** Stack's compiler accepts `provider: Any | None = None` but never calls it. The plan calls for an LLM-led compiler graph (source plan §6).

**Fix scoped for pass 1:**

- Keep the deterministic path as the default. Add a single integration point: when `provider is not None`, call `compiler_graph.run(goal, runtime_spec, registry, provider) -> CompilerProposal` and use that proposal to **select** which families and claim packs the deterministic body emits. The provider proposes; deterministic code remains the only writer of the final `OraclePackage`. This matches source plan §6.2 ("specialist outputs are proposals").
- Wire `compiler_graph.run` to a stub that returns a proposal containing exactly the deterministic defaults when no provider is set — so the deterministic path is just `compile(provider=None)`.
- Defer real LangGraph wiring of the compiler graph to pass 2 (§16).
- Gate domain-specific claim packs behind `family.score_applicability(context)` rather than substring matching on the goal text. Trigger words (`"trading"`, `"repo"`, etc.) live in `family.score_applicability`, not in the compiler core. Adding a new domain = registering a family, no compiler edit.

### 7.4 Stack's loader diff has an indentation bug

**Symptom:** `agintor_full_oracle_langgraph_stack/existing_edits/agintor__runtime__loader.py.search_replace.diff:22-37` wraps the `for key, module_ref in manifest.policy_modules.items():` loop in an `else:` branch but only re-indents three lines of the loop body in the next block. The intermediate lines (`module_path = …`, `module = _load_module(…)`, etc.) are left at the original indent and become unreachable / SyntaxError.

**Verified against live repo:** `agintor/runtime/loader.py:405-414` contains the multi-statement loop body the diff would orphan.

**Fix:** Re-author this edit by hand. The replacement should use a single SEARCH that matches the entire policy-modules block (including the body that hashes sources and counts AST nodes) and a REPLACE that wraps the whole block in `if runtime_kind in {…}:` / `else:`, indenting every line of the original body by one level inside the `else:` branch. Verify by `python -m compileall agintor/runtime/loader.py` before committing.

### 7.5 Patch's `public_oracle_projection` leaks sealed validator metadata

**Symptom:** `agintor_full_plan_patch/new_files/agintor/oracle/projections.py:53-77` blanks `inputs/outputs_schema/health_tests` for sealed validators but leaves `validator_id`, `family_id`, `claim_ids`, `independence_group`, `authority_ceiling`, `failure_action` in the public payload.

**Fix:** Do not use Patch's `projections.py`. Stack's `oracle_public_projection` in historical `contracts/oracle.py:320-376` drops sealed validators entirely and only includes those with `visibility == "public"`. Re-export it from `agintor/oracle/projections.py`:

```python
from ..contracts.oracle import oracle_public_projection as public_oracle_projection
from ..contracts.oracle import oracle_sealed_projection as sealed_oracle_projection

__all__ = ["public_oracle_projection", "sealed_oracle_projection"]
```

Then the rest of the codebase can keep importing from `agintor.oracle.projections` per source plan §13 module map.

### 7.6 `__pycache__` pollution

**Symptom:** Stack ships `__pycache__/` inside `new_files/`. Copying the bundle naively contaminates the repo with bytecode for Python 3.13 (the user is on 3.12).

**Fix:** When implementing §8 below, use a copy filter that excludes `__pycache__/` and `*.pyc`. A one-liner:
```powershell
python -c "import shutil; shutil.copytree(r'<src>', r'<dst>', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))"
```

### 7.7 Stack's evaluator diff inserts a duplicate import block

**Symptom:** `agintor_full_oracle_langgraph_stack/existing_edits/agintor__evaluation__evaluator.py.search_replace.diff:159-165` does a SEARCH on `from ..oracle.qa import OracleQARunner\nfrom ..oracle.package_io import load_oracle_package` and REPLACEs with the same lines plus `from .oracle_runner import OracleEvaluationRunner`. The earlier SEARCH/REPLACE in the same diff (lines 21-24) already added those two imports. Applying them in the diff order means the second SEARCH will fail to find a unique match.

**Fix:** Collapse the two import-related search/replace blocks into one SEARCH/REPLACE that targets the original `from ..utils import ensure_directory, stable_hash` line (verified present in `agintor/evaluation/evaluator.py:40`) and inserts all three new imports at once. Apply this manually.

### 7.8 `baseline_langgraph_runtime_spec()` helper export/repair

**Symptom:** Both bundles assume `default_langgraph_runtime_spec` (Patch) or `baseline_langgraph_runtime_spec` (Stack) is exported from `agintor.contracts`. Stack defines a baseline helper in its `runtime_spec.py`; Patch relies on a differently named default helper. The synthesis must converge on one exported helper and must not duplicate competing constructors.

**Fix:** Use `baseline_langgraph_runtime_spec()` as the single helper name. If Stack's implementation is copied, verify the helper is present, remove any copied schema marker field, and export it from `agintor/contracts/__init__.py`. If the copied file lacks it, add the function to `agintor/contracts/runtime_spec.py`:

```python
def baseline_langgraph_runtime_spec(
    *,
    runtime_id: str,
    name: str = "Baseline LangGraph Runtime",
) -> RuntimeSpec:
    """Construct the canonical default RuntimeSpec for `init-runtime --runtime-kind langgraph_spec`."""
    return RuntimeSpec(
        runtime_id=runtime_id,
        runtime_kind="langgraph_spec",
        name=name,
        description="Baseline spec-backed runtime; one direct-response agent terminating immediately.",
        agents=[
            AgentSpec(
                agent_id="agent.default",
                role="worker",
                prompt=PromptSpec(task_template="{prompt}"),
            ),
        ],
        graph=GraphSpec(
            graph_id="runtime_graph",
            entry_node="node.default",
            terminal_nodes=["node.terminal"],
            nodes=[
                GraphNodeSpec(
                    node_id="node.default",
                    node_type="direct_response",
                    agent_id="agent.default",
                    output_key="answer",
                ),
                GraphNodeSpec(
                    node_id="node.terminal",
                    node_type="verify",
                    input_keys=["answer"],
                ),
            ],
            edges=[GraphEdgeSpec(source="node.default", target="node.terminal")],
        ),
        tools=[],
        models=[
            ModelPolicy(
                model_policy_id="default",
                provider_name="runtime_default",
                model_class="small",
            ),
        ],
        memory=MemoryPolicy(memory_policy_id="default", memory_kind="short_term"),
        execution=ExecutionPolicy(max_steps=32, side_effect_policy="receipt_required"),
        tracing=TracingPolicy(trace_level="full"),
        mutation_history=[],
        metadata={"template": "baseline_runtime_langgraph"},
    )
```

Export it from `agintor/contracts/__init__.py` alongside the other `runtime_spec` exports added in §8.2.

### 7.9 Runtime kernel bundling fails in clean worktrees

**Symptom:** Stack's `RuntimeSpecCompiler.compile_to_directory()` calls `bundle_runtime_kernel(...)`. In a clean worktree, `agintor/templates/baseline_runtime/runtime_profile.json` may be absent because the directory is gitignored / generated. spec-backed export then fails with `FileNotFoundError`.

**Implemented resolution:** option 1 landed. The tracked default now lives at `agintor/runtime/sdk/defaults/runtime_profile.json`; the ignored generated template is not required in a clean checkout.

**Historical options considered:**

1. **Move required runtime resource defaults into tracked package data.** Create `agintor/runtime/sdk/defaults/runtime_profile.json` (tracked) and have `bundle_runtime_kernel(...)` read from it when the ignored template file is absent.
2. **Generate the required runtime profile resource during export.** Call `agintor.runtime.profile.synthesize_default_profile()` (new) inside `compile_to_directory` before `bundle_runtime_kernel(...)`.
3. **Teach `bundle_runtime_kernel(...)` to synthesize the default runtime profile payload when the ignored template file is absent.** Inline fallback inside the bundler.

Option 1 was implemented and covered by the retained clean-worktree export tests.

---

## 8. Historical file-by-file action map

This records the original landing sequence. It is not a current implementation checklist.

### 8.1 Phase A — pure additions (no existing-file edits, no deps)

**Goal:** land the new contracts and helpers; keep `python -m pytest` green by NOT yet exporting them from `agintor.contracts.__init__`.

Copy these files from the named bundle into the live repo, filtering `__pycache__`/`*.pyc`:

| Destination | Source |
|---|---|
| `agintor/contracts/runtime_spec.py` | Stack `new_files/agintor/contracts/runtime_spec.py`, then apply §7.1 + §7.8 + §4 (private/sealed scanner) fixes |
| `agintor/contracts/spec_actions.py` | Stack, then apply §4 (scanner applied to patch payloads) |
| `agintor/contracts/oracle.py` | Stack |
| `agintor/oracle/__init__.py` | Stack |
| `agintor/oracle/package_io.py` | **Patch** (then rewire to Stack projection per §7.5) |
| `agintor/oracle/projections.py` | New shim — see §7.5 |
| `agintor/oracle/qa.py` | Stack |
| `agintor/oracle/validator_registry.py` | Stack, enforce §3 family interface |
| `agintor/oracle/subagents.py` | Stack |
| `agintor/oracle/compiler.py` | Stack, then apply §7.2 + §7.3 fixes |
| `agintor/oracle/compiler_graph.py` | Stack |
| `agintor/oracle/families/__init__.py` | Stack |
| `agintor/oracle/families/exact_private_answer.py` | Stack |
| `agintor/oracle/families/schema_artifact.py` | Stack |
| `agintor/oracle/families/repo_patch.py` | Stack |
| `agintor/oracle/families/stateful_service.py` | Stack |
| `agintor/oracle/families/trace_state.py` | Stack |
| `agintor/oracle/families/factual_grounded.py` | Stack |
| `agintor/oracle/families/pairwise_preference.py` | Stack |
| `agintor/oracle/families/trading_outcome.py` | Stack (evidence list per §18) |
| `agintor/oracle/families/human_audit.py` | Stack |
| `agintor/oracle/families/inspect_runner.py` | Stack (stub `_run` in pass 1) |
| `agintor/oracle/families/openai_eval_runner.py` | Stack (stub `_run` in pass 1) |
| `agintor/oracle/families/consent_proof.py` | Stack |
| `agintor/runtime/langgraph/__init__.py` | Stack |
| `agintor/runtime/langgraph/state.py` | Stack (solve-time, §6) |
| `agintor/runtime/langgraph/operation_service.py` | Stack (solve-time, §5, §6) |
| `agintor/runtime/langgraph/checkpointing.py` | Stack (solve-time stub, §6, real embedding deferred per §16) |
| `agintor/runtime/langgraph/adapters.py` | Stack (solve-time, §6) |
| `agintor/runtime/langgraph/compiler.py` | Stack factory/export compiler only (§6); move solve-time executor symbols out if Stack imports them from this file |
| `agintor/runtime/langgraph/executor.py` | New solve-time module holding `CompiledSpecRuntime`, `compile_runtime_spec`, backend selection, and runtime-spec file constants (Stack + Patch LangGraph build path) |
| `agintor/runtime/langgraph/entrypoint.py` | Stack (solve-time, §6) |
| `agintor/evaluation/oracle_runner.py` | Stack |
| `agintor/search/spec_mutator.py` | Stack |
| `agintor/integrations/tradingagents/__init__.py` | Stack |
| `agintor/integrations/tradingagents/spec.py` | Stack |
| `agintor/integrations/tradingagents/adapter.py` | Stack |
| `agintor/integrations/tradingagents/action_mapper.py` | Stack |
| `agintor/integrations/tradingagents/data_snapshots.py` | Stack |
| `agintor/integrations/tradingagents/ledgers.py` | Stack |
| `agintor/integrations/tradingagents/validators.py` | Stack (evidence per §18) |
| `agintor/integrations/tradingagents/outcome_oracle_family.py` | Stack |
| `agintor/integrations/tradingagents/compiler.py` | **Patch** (rewired to Stack profile) |
| `agintor/templates/baseline_runtime_langgraph/runtime_spec.json` | **Patch**, with `entry_node`/`terminal_nodes` rename (§1 row) |
| `agintor/templates/baseline_runtime_langgraph/langgraph_app.py` | **Patch** |
| `agintor/templates/baseline_runtime_langgraph/README.md` | Stack |

**Exit gate A:** `python -m compileall -q agintor`. No pytest changes yet.

### 8.2 Phase B — wire the new contracts into `agintor.contracts`

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

Add to the `_FORWARD_REF_NAMESPACE` model rebuild block (after `PromotionDecision,`):

```python
    RuntimeSpec,
    SpecAction,
    OraclePackage,
```

(`OracleEvaluationSummary` exists in Patch's `oracle.py` but not Stack's — do not export it; Stack delivers the same payload through `OracleEvaluationRunner` results, which are not Pydantic models needing forward-ref rebuild.)

**Exit gate B:**
```powershell
python -m compileall -q agintor
python -c "from agintor.contracts import RuntimeSpec, OraclePackage, SpecAction, baseline_langgraph_runtime_spec"
```

### 8.3 Phase C — thread oracle/runtime identity through evidence ledgers

This used Stack's `agintor__contracts__evidence.py.search_replace.diff`. Anchors were verified against the then-live repo: `EvidenceRecord`, `PairedComparison`, `ProgressSignal`, and `PromotionDecision` had the field shape the diff assumed.

The implementation used Stack's `agintor__contracts__runtime.py.search_replace.diff` (`RuntimeManifest` extras: `runtime_kind`, `runtime_spec_path`, `runtime_spec_digest`, `oracle_package_hash` — all with defaults so existing policy-module manifests loaded unchanged). The historical anchor was `agintor/contracts/runtime.py:16-22`.

The implementation used Stack's `agintor__contracts__search.py.search_replace.diff` to add `oracle_package_hash` to archive/search records.

**Exit gate C:** `python -m pytest tests/test_evidence_ledger*.py` (whatever the existing repo names them) stays green. Existing fields are additive with defaults so no test should regress.

### 8.4 Phase D — runtime spec compilation and host loader

1. **`agintor/runtime/langgraph/compiler.py` + solve-time executor module** — final shape: Stack's factory/export compiler remains in `compiler.py`; `CompiledSpecRuntime`, `compile_runtime_spec`, backend selection, and constants imported by adapters/generated apps move to `executor.py` (or an equivalently isolated solve-time module). The solve-time executor uses Stack base behavior plus Patch's `StateGraph` build path. Its class structure becomes:

   ```python
   class CompiledSpecRuntime:
       def __init__(self, runtime_spec, *, provider=None):
           self.runtime_spec = runtime_spec
           self.service = RuntimeOperationService(runtime_spec, provider=provider)
           self._lg_app = self._build_langgraph_app()
           self.backend = "langgraph" if self._lg_app else "sequential"

       def _build_langgraph_app(self):
           try:
               from langgraph.graph import StateGraph
           except Exception:
               return None
           graph = StateGraph(dict)  # Pass 1: dict state; pass 2 may switch to typed schema
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
               return LangGraphRuntimeState(
                   **dict(self._lg_app.invoke(initial_state(prompt, **kwargs)))
               )
           return self._invoke_sequential(prompt, **kwargs)
   ```

   Both backends share `RuntimeOperationService`, so node behavior is identical regardless of which path runs. Apply the LangGraph API subset constraint per §13.7 — no conditional edges, no parallel branches, no interrupt/resume.

2. The implementation used Stack's historical `agintor__runtime__loader.py.search_replace.diff` after rewriting it per §7.4. The corrected edit targeted:

   - Add `runtime_spec: Any | None = None` to `LoadedRuntime`.
   - Add the `from .langgraph.adapters import build_spec_policy_objects, load_runtime_spec` import.
   - Add the runtime-spec digest fingerprint to `immutable_fingerprints` (Stack's `RUNTIME_PROFILE_FILE` block edit, lines `agintor/runtime/loader.py:368-374`).
   - Wrap the policy-modules loop body (`agintor/runtime/loader.py:405-414`) correctly inside `if runtime_kind in {"langgraph_spec", "tradingagents_langgraph"}: ... else: ...`. Verify with `python -m compileall agintor/runtime/loader.py` before committing.

3. Apply the runtime kernel bundling fix per §7.9 (choose option 1 unless there's a strong reason otherwise). Land the clean-worktree test.

4. Add the spec-backed fast path in `agintor/runtime/sdk/entrypoint.py` or `TaskRuntime.run_task()` per §6. Do this before relying on the loader compatibility objects; otherwise the runtime can load a spec but still execute through the old policy-module kernel.

**Historical exit gate D (the paths below predate test consolidation and are not a current command):**
```powershell
python -m compileall -q agintor
python -c "from agintor.runtime.langgraph.executor import CompiledSpecRuntime; from agintor.contracts import baseline_langgraph_runtime_spec; CompiledSpecRuntime(baseline_langgraph_runtime_spec(runtime_id='r1')).invoke('hi')"
python -m pytest tests/test_runtime_host.py tests/test_runtime_execution.py tests/test_langgraph_export_clean_worktree.py tests/test_runtime_langgraph_solve_time_imports.py
```

### 8.5 Phase E — evaluator threads `OraclePackage` into ledgers + actually runs validators

The implementation used Stack's historical `agintor__evaluation__evaluator.py.search_replace.diff` after consolidating the duplicate import block per §7.7.

Stack's diff does five load-bearing things; verify each landed:

1. `RuntimeEvaluator.__init__` accepts `oracle_package: OraclePackage | str | Path | None`.
2. `_oracle_identity_payload()` adds `oracle_package_hash`, `runtime_spec_digest`, `oracle_public_view_hash`, `oracle_sealed_view_hash` to evidence digests and ledger rows.
3. New `staged_evaluate_runtime_pair(parent_dir, child_dir, objective, mutation_action_ids=())` for spec-runtime pair evaluation.
4. New per-run call: `OracleEvaluationRunner.evaluate_run(self.oracle_package, run, sealed_payload)` populates `validator_results` and `claim_results` on `EvidenceRecord`. Sealed payload contract per §13.6.
5. Pre-flight `OracleQARunner.run(self.oracle_package)` short-circuits Stage 4 with `oracle_package_qa: fail` if QA does not pass.

The implementation used Stack's historical `agintor__evaluation__progress_oracle.py.search_replace.diff`. It carried `oracle_package_hash`, `parent_runtime_spec_digest`, and `child_runtime_spec_digest` into `ProgressSignal` and `PromotionDecision`, with the comparison-blocking rule from source plan §10.3 and the policy-module↔spec-backed boundary rule from §13.4:

```python
# Inside ProgressOracle.decide(), before any axis comparison:
parent_hash = decision_input.parent_oracle_hash or ""
child_hash = comparison.oracle_package_hash or ""
if parent_hash and child_hash and parent_hash != child_hash:
    return self._quarantine(comparison, reason_code="oracle_package_hash_mismatch")
if bool(parent_hash) != bool(child_hash):
    # policy-module↔spec-backed boundary: one side has a hash, the other does not
    return self._quarantine(comparison, reason_code="oracle_package_hash_mismatch_runtime_kind_boundary")
```

(The exact field path will depend on how `ProgressOracle.decide` reads parent state in the live repo. Read the existing function before adding this; preserve its existing decision routing.)

**Exit gate E:**
```powershell
python -m pytest tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py
python -m pytest tests/test_oracle_evaluation_runner.py tests/test_progress_oracle_oracle_hash_block.py
```

(must stay green; new fields default to empty so existing pairs still compare).

### 8.6 Phase F — search engine + archive use the spec mutator and oracle hash

The implementation used Stack's historical `agintor__search__engine.py.search_replace.diff` and verified the `parent_loaded_runtime` branch:

```python
if str(getattr(parent_loaded_runtime.manifest, "runtime_kind", "policy_modules")) in {
    "langgraph_spec", "tradingagents_langgraph"
}:
    spec_candidate = (self.provider_spec_mutator or self.spec_mutator).mutate(
        SpecMutationContext(...)
    )
    stage_results = self.evaluator.staged_evaluate_runtime_pair(
        parent_dir,
        spec_candidate.child_runtime_dir,
        objective,
        mutation_action_ids=...,
    )
```

This is the only place where spec-backed evolution branches off the policy-module patch-mutator path.

The implementation used Stack's historical `agintor__search__archive.py.search_replace.diff` to add `oracle_package_hash` and `runtime_spec_digest` to archive rows.

**Exit gate F:**
```powershell
python -m pytest tests/test_search_*.py
python -m pytest tests/test_spec_mutator.py
```

(existing policy-module search must stay green.)

### 8.7 Phase G — factory export wires spec-backed and writes the oracle package

The implementation used Stack's historical `agintor__factory__export.py.search_replace.diff`. After applying it:

- `_write_seed_runtime` accepts `runtime_kind` and `goal_spec`.
- For `langgraph_spec` / `tradingagents_langgraph`, it calls `RuntimeSpecCompiler().compile_to_directory(runtime_spec, seed_runtime_dir, force=True)` and writes the oracle package via `write_oracle_package(package, seed_runtime_dir / "oracle")`.

**Verify the export does not include sealed material.** This is invariant 5 of §2. The exported runtime directory must contain `oracle/public.json` (from `oracle_public_projection`) but never `oracle/sealed.json`. The sealed projection is written to a separate evaluator-only path under `.agintor_runs/.../oracle_packages/`.

Patch's `package_io.py` already has the public/sealed split. After this phase, audit the exported runtime directory contents with the test added in §11 (`test_export_no_sealed_material`).

The implementation used Stack's historical `agintor__factory__planning.py.search_replace.diff` to add the `OracleCompiler` import, then added the `OracleCompiler().compile(goal_spec, runtime_spec)` call and stored package refs/hashes in build summaries and factory artifacts.

Persist `runtime_kind` through the factory chat/build state per §13.10. The selected kind must appear in `GoalSpec.constraints` (or the existing equivalent constraints bag), `RuntimePlan`, build/export summaries, and factory message/identity state. Follow-ups reuse the pinned kind; attempts to switch kind inside the same factory chat fail clearly.

**Exit gate G:**
```powershell
agintor build-runtime "demo: respond with the prompt verbatim" --destination .tmp_langgraph_runtime --runtime-kind langgraph_spec --steps 0
python -m pytest tests/test_export_no_sealed_material.py tests/test_factory_runtime_kind_pinning.py
```

### 8.8 Phase H — CLI surfaces

The implementation used Stack's historical `agintor__cli.py.search_replace.diff`. It added:

- `init-runtime --runtime-kind` → routes through `RuntimeSpecCompiler.compile_to_directory()` for spec-backed kinds.
- `eval --oracle-package` → loads a frozen package via `load_oracle_package()` and passes it to the evaluator.
- `oracle-qa <package_dir>` → runs QA, exits non-zero on fail.
- `inspect-oracle <package_dir> [--public | --sealed]` → prints public projection by default; `--sealed` requires an explicit flag and only prints sealed content for evaluator-side inspection.

Add one CLI surface the Stack diff misses: `compile-oracle <goal> <destination>` (Patch had this; useful for hand-driving the compiler):

```python
@app.command("compile-oracle")
def compile_oracle_cmd(
    goal: str,
    destination: str,
    runtime_kind: str = typer.Option("langgraph_spec", "--runtime-kind"),
) -> None:
    goal_spec = GoalSpec(
        goal_id=f"goal.{abs(hash(goal))}",
        raw_prompt=goal,
        normalized_goal=goal.strip(),
    )
    runtime_spec = baseline_langgraph_runtime_spec(runtime_id="runtime.preview")
    package = OracleCompiler().compile(goal_spec, runtime_spec)
    frozen = write_oracle_package(package, destination)
    typer.echo(json.dumps({
        "package_id": frozen.package_id,
        "package_hash": frozen.package_hash,
        "destination": destination,
    }, indent=2, sort_keys=True))
```

Existing command defaults must not change. CLI must not print sealed package contents unless the user explicitly asks for `--sealed`.

**Exit gate H:**

- `agintor init-runtime .tmp_langgraph_dir --runtime-kind langgraph_spec --force` succeeds.
- `agintor compile-oracle "demo goal" .tmp_pkg --runtime-kind langgraph_spec` produces `oracle/public.json` and `oracle/sealed.json` with matching `package_hash`.
- `agintor oracle-qa .tmp_pkg` exits 0.
- `agintor inspect-oracle .tmp_pkg --public` does not contain any of: `private_expected`, `sealed_inputs`, `sealed_fixture_refs`, `hidden_tests`, `promotion_thresholds`, `private_rubric`.
- `agintor inspect-oracle .tmp_pkg` (no flag) defaults to public-safe output.
- `agintor inspect-oracle .tmp_pkg --sealed` prints sealed content (and warns it is evaluator-only).

### 8.9 Phase I — optional dependencies

The implementation used Stack's historical `pyproject.toml.search_replace.diff`. It added:

- `langgraph = ["langgraph"]`
- `inspect = ["inspect-ai>=0.3"]`

Plus:

- `langchain` only when generated agent nodes actually use LangChain agent helpers (pass 2 likely).
- Keep Inspect/OpenAI eval runners optional and lazy.
- The base install must not require LangGraph.

**Exit gate I:** `pip install -e ".[langgraph]"` succeeds; the test suite stays green both with and without the extra installed.

---

## 9. New module conventions

When editing the copied files in Phase A, enforce these:

- **Historical pass constraint: no comments explaining what the code does.** Only keep comments that name a non-obvious invariant (e.g., `# MutationActionRef.created_at is excluded from canonical payload — see §7.1`).
- **All Pydantic models inherit from one of the existing base models** (`RuntimeSpecModel`, `OracleModel`). Do not introduce a third model base.
- **No runtime/schema marker fields or unions** — runtime kinds are plain strings. Disposable artifacts do not need compatibility markers (AGENTS.md).
- **If copied LangGraph/oracle/trading code introduces numbered identifiers, fix them repo-wide.** Examples: runtime kinds, template directories, helper names, capability flags, stable-hash namespaces, generated app imports, tests, CLI defaults, and bundle manifests. Use `rg` plus a mechanical rename, or delegate a bounded cleanup, but do not leave two names that differ only by a number. Do not churn unrelated existing persisted IDs such as checkpoint/state-store schema metadata or benchmark challenge IDs unless this work directly changes that surface.
- **`stable_hash` is the only hash function.** Do not introduce a parallel hashing helper.
- **No silent exception swallowing.** Validator exceptions become `ValidatorResult(status="error")`; package-level invalidity raises explicitly.
- **No new ABI/storage axes.** `RUNTIME_CONTRACT_VERSION` remains the only runtime-loading contract gate (§13.1).

---

## 10. Pyflakes + import hygiene

After Phase A, run this if `pyflakes` is available:

```powershell
python -m pyflakes agintor/oracle agintor/runtime/langgraph agintor/integrations/tradingagents agintor/contracts/runtime_spec.py agintor/contracts/spec_actions.py agintor/contracts/oracle.py
```

Both bundles import symbols they do not use (`EvidenceRef` in Patch's `oracle.py`, several `from typing import …` extras in Stack). Strip these. After Phase D, re-run for the runtime LangGraph modules. After Phase E, re-run for `agintor/evaluation/`.

If `pyflakes` is not installed in the local dev environment, do not block on the command unless you add it to the dev dependencies. `python -m compileall -q agintor` plus the focused pytest slices remain mandatory.

---

## 11. Historical tests-to-land map

This table and its copy-paste commands record the original regression plan and are not runnable as a current test list. Current consolidated coverage lives principally in `tests/test_langgraph_oracle_pass1.py`, `tests/test_langgraph_backend_parity.py`, `tests/test_oracle_phase0_authority.py`, `tests/test_oracle_phase1_validation_contracts.py`, `tests/test_repo_patch_proof_lane.py`, and `tests/test_import_boundaries.py`, plus retained clean-worktree, evolution, and factory-pinning tests.

| Test file | What it must assert |
|---|---|
| `tests/test_runtime_spec.py` | `RuntimeSpec.model_validate(spec.model_dump())` is field-equal. Graph validators reject duplicate node IDs, missing entry, missing terminals. **§4: forbidden keys rejected, legitimate strings allowed.** |
| `tests/test_runtime_spec_digest_stability.py` | Roundtripping the baseline spec through JSON 10× yields the same `runtime_spec_digest`. Mutating `mutation_history[*].created_at` does not change the digest. |
| `tests/test_spec_actions.py` | Each action type validates against a parent spec. Adding an agent updates the child digest. Action ledger lines are JSON-serializable. **§4: patch with forbidden key rejected.** |
| `tests/test_oracle_package.py` | Constructing an `OraclePackage` with a hard claim that has no validator and no `unverifiable_reason` raises. `package_hash`, `public_view_hash`, `sealed_view_hash` are auto-derived and stable across roundtrips. |
| `tests/test_oracle_public_projection.py` | Public projection contains no sealed answer material. If `expected` is present, it contains `oracle_claim_ids` only. Projection never contains keys: `private_expected`, `sealed_inputs`, `sealed_fixture_refs`, `hidden_tests`, `promotion_thresholds`, `private_rubric`. **Sealed validators are absent entirely** (regression for §7.5). |
| `tests/test_oracle_sealed_eval.py` | Evaluator path through `OracleEvaluationRunner` produces one `ValidatorResult` per `validator_spec` and one `ClaimResult` per claim. |
| `tests/test_oracle_qa.py` | `OracleQARunner` rejects packages that fail leakage scan, contain uncovered hard claims, or have `package_hash` not matching the recomputed value. |
| `tests/test_oracle_evaluation_runner.py` | Runner emits validator and claim results. Validator exception becomes `ValidatorResult(status="error")`. Hard claim fails when its validator fails. Unsupported family abstains with `A0` authority. Result digests are stable. |
| `tests/test_validator_registry.py` | Each registered family implements the §3 interface fully. `score_applicability` returns a float in [0,1]. Each family has one positive control and one negative control. |
| `tests/test_langgraph_runtime_compiler.py` | `RuntimeSpecCompiler.compile_to_directory(spec, dir)` writes `runtime_spec.json`, `runtime_manifest.json`, `deployment_contract.json`, the generated app file, and bundles the runtime kernel. `CompiledSpecRuntime(spec).invoke("hi")` returns a state with `status == "completed"`. When `langgraph` is installed, `CompiledSpecRuntime(spec).backend == "langgraph"`. Without it, `backend == "sequential"`. The generated app imports the solve-time executor, not the factory/export compiler. |
| `tests/test_runtime_langgraph_solve_time_imports.py` | The solve-time modules listed in §6 do not import `agintor.search.*`, `agintor.evaluation.*`, `agintor.factory.*`, or `agintor.oracle.*`, and they do not import the factory/export compiler module. |
| `tests/test_langgraph_resume_capability.py` | spec-backed runtime capability exchange reports checkpoint/resume support as false, host resume fails clearly, and policy-module runtime capability behavior is unchanged. (§13.2) |
| `tests/test_langgraph_export_clean_worktree.py` | `bundle_runtime_kernel(...)` succeeds in a clean worktree without any local `agintor/templates/baseline_runtime/` contents (§7.9). |
| `tests/test_spec_mutator.py` | Heuristic mutator emits a `SpecActionMutationResult` with at least one `SpecAction`, a child runtime dir whose spec digest differs from the parent, and a mutation ledger row. |
| `tests/test_export_no_sealed_material.py` | After `agintor build-runtime ... --runtime-kind langgraph_spec`, the destination directory has `oracle/public.json` and never `oracle/sealed.json`, never `private_expected` strings in any file, never `promotion_threshold`, never `hidden_tests`. |
| `tests/test_tradingagents_adapter.py` | `TradingAgentsRuntimeSpec.compile_to_directory()` produces a runtime that loads. Outcome validator runs against a synthetic decision/order/fill ledger and emits a `pass`/`fail` `ValidatorResult`. Trading-specific validators absent from a non-trading goal compilation. |
| `tests/test_trading_oracle_package.py` | Trading goal compiles to a package containing `trading_outcome` claims. Non-trading goal does not. |
| `tests/test_progress_oracle_oracle_hash_block.py` | `ProgressOracle` quarantines a parent/child pair with mismatched `oracle_package_hash`. Also covers the policy-module↔spec-backed boundary case (§13.4). policy-module→policy-module comparisons (both sides empty) pass through. |
| `tests/test_langgraph_evolve_loop_integration.py` | **Integration test.** Marked `@pytest.mark.integration`. Exercises: `init-runtime --runtime-kind langgraph_spec` → seeded `OraclePackage` → one heuristic spec mutation → `staged_evaluate_runtime_pair` → `ProgressOracle.decide` → archive update assertion. |
| `tests/test_provider_precedence_langgraph.py` | `agintor solve --provider openai` overrides spec `models[].provider_name` at the `RuntimeOperationService` call site. |
| `tests/test_factory_runtime_kind_pinning.py` | Factory chat follow-ups preserve the originally selected `runtime_kind`; attempts to switch runtime kind in the same factory chat fail clearly and do not rewrite the project state. (§13.10) |
| `tests/test_verifier_oracle_package_dispatch.py` | A `BenchmarkTask` with `verifier_type="oracle_package"` routes through `RuntimeEvaluator` to `OracleEvaluationRunner.evaluate_run(...)`, never through the local JSON-exact verifier. Also assert `agintor/contracts/verifiers.py` does not import `agintor.evaluation`. (§13.5) |

**Historical test-slice command** (retained as execution provenance; do not run as a current suite):

```powershell
.\.venv\Scripts\python -m compileall -q agintor
.\.venv\Scripts\python -m pytest tests/test_runtime_spec.py tests/test_runtime_spec_digest_stability.py tests/test_spec_actions.py tests/test_oracle_package.py tests/test_oracle_public_projection.py tests/test_oracle_qa.py tests/test_oracle_sealed_eval.py tests/test_oracle_evaluation_runner.py tests/test_validator_registry.py tests/test_langgraph_runtime_compiler.py tests/test_runtime_langgraph_solve_time_imports.py tests/test_langgraph_resume_capability.py tests/test_langgraph_export_clean_worktree.py tests/test_spec_mutator.py tests/test_progress_oracle_oracle_hash_block.py tests/test_factory_runtime_kind_pinning.py tests/test_verifier_oracle_package_dispatch.py
```

The original implementation then ran the repository's then-current load-bearing slices:

```powershell
.\.venv\Scripts\python -m pytest tests/test_runtime_host.py tests/test_runtime_execution.py tests/test_progress_oracle.py tests/test_evaluator_progress_gates.py tests/test_pairwise_comparator.py tests/test_search_*.py tests/test_runtime_builder.py
```

Integration tests last:

```powershell
.\.venv\Scripts\python -m pytest -m integration --basetemp .tmp_pytest_integration
```

---

## 12. Apply order summary

1. **Phase A** — copy new files (filter `__pycache__`/`*.pyc`); apply §7.1, §7.2, §7.3, §7.5, §7.8, §4 (scanner), §6 (solve-time imports), §3 (validator interface enforcement) in-file. Land `tests/test_runtime_langgraph_solve_time_imports.py`.
2. **Phase B** — wire `agintor.contracts.__init__`.
3. **Phase C** — evidence ledger identity threading.
4. **Phase D** — runtime spec compiler + solve-time executor + corrected loader (§7.4) + LangGraph build path + spec-backed runtime entry fast path + kernel bundling fix (§7.9). Land `tests/test_langgraph_export_clean_worktree.py`.
5. **Phase E** — evaluator + progress oracle, with corrected import block (§7.7), the comparison-block rule, and the policy-module↔spec-backed boundary rule (§13.4). Land `tests/test_verifier_oracle_package_dispatch.py`.
6. **Phase F** — search engine + archive.
7. **Phase G** — factory export and runtime-kind pinning through factory chat state. Land `tests/test_export_no_sealed_material.py` and `tests/test_factory_runtime_kind_pinning.py`.
8. **Phase H** — CLI.
9. **Phase I** — pyproject extras.
10. **Final** — integration test `tests/test_langgraph_evolve_loop_integration.py` and provider precedence test `tests/test_provider_precedence_langgraph.py`.

After each phase, run `python -m compileall -q agintor` and the relevant test slice. Do not advance to the next phase until the prior one is green.

---

## 13. Open architectural decisions resolved for pass 1

Both source plans left these gaps. They are decided here so the implementer does not make ad-hoc judgment calls.

### 13.1 Runtime identity vs existing runtime contract

Do not add extra schema marker fields, numeric runtime kinds, or new ABI/storage axes. The single existing `RUNTIME_CONTRACT_VERSION` continues to gate runtime loading. policy-module manifests load with `runtime_kind="policy_modules"` (default), spec-backed manifests with `runtime_kind="langgraph_spec"` or `runtime_kind="tradingagents_langgraph"` — all under the same `RUNTIME_CONTRACT_VERSION`. `_validate_runtime_contract(...)` in `agintor/runtime/loader.py:33-42` continues to check only `runtime_contract_version`.

If an implementation step touches a copied bundle symbol that still has a planning-stage number in it, rename that symbol across the repo surface being introduced in this pass. Do not merely remove the number in one call site. After the rename, search for the old name and for the generic patterns `*_v1`, `*_v2`, `v1_*`, `v2_*`, and `schema_version` inside the touched LangGraph/oracle/trading files and tests. Leave unrelated existing persisted IDs alone unless the current change has made them part of the new runtime/oracle contract.

### 13.2 WS3 / checkpoint embedding (deferred)

spec-backed runtimes are non-resumable in pass 1. `agintor/runtime/langgraph/checkpointing.py` ships as a stub that returns `not_supported` for resume.

Reflect that honestly in runtime capability metadata until WS3 lands: `CapabilityExchange.checkpoint_support=False`, `CapabilityExchange.resume_support=False`, and `runtime_asset_capabilities["checkpoints"]=False` for spec-backed runtimes. Host resume against a spec-backed runtime must fail clearly instead of advertising support that cannot work.

Documented integration site for pass 2 (add to deferred ledger):

- `agintor/runtime/kernel/checkpointing/snapshots.py` — extend the snapshot payload to embed `LangGraphRuntimeState` inside the existing `CheckpointEnvelope`.
- `agintor/runtime/kernel/checkpointing/restore.py` — extend restore to detect spec-backed envelopes and rehydrate `LangGraphRuntimeState` via `agintor/runtime/langgraph/state.py:from_envelope_payload`.
- Coordinate with WS3 lead before pass 2 starts.

### 13.3 OracleCompiler invocation lifecycle

Compile **once per `GoalSpec`**. The compiler is invoked at `agintor/factory/planning.py` time, the resulting frozen `OraclePackage` is stored alongside the build artifacts, and the same package travels through the entire evolution loop for that goal.

A new package is only created when:

- The user amends the goal (`GoalSpec.amendment_index` advances).
- A `signal_sufficiency` gap is detected in evidence accumulation (source plan §12.2).

When a new package is created, both parent and child must be re-evaluated under the new package before any comparison. Existing comparisons under the old package hash become diagnostic.

### 13.4 policy-module↔spec-backed comparison

When one side of a pair has empty `oracle_package_hash` and the other has a non-empty value, `ProgressOracle` returns `quarantine` with reason `oracle_package_hash_mismatch_runtime_kind_boundary`. No special bridging in pass 1. policy-module→policy-module comparisons (both sides empty) continue to use the existing axis-delta logic unchanged.

### 13.5 `verifier_type="oracle_package"` dispatch

The dispatch lives in `RuntimeEvaluator`, not in `agintor/contracts/verifiers.py`. `contracts/verifiers.py` is shared by runtime host, Docker paths, CLI rescoring, and evaluator code, so it must not import `agintor.evaluation` or `OracleEvaluationRunner`. For `verifier_type="oracle_package"`, the evaluator builds the sealed payload (§13.6) and routes to `OracleEvaluationRunner.evaluate_run(package, run, sealed_payload)`.

`contracts/verifiers.py` may recognize `oracle_package` only as "not locally scorable" and return a diagnostic/no-score result if a local rescoring path encounters it without an evaluator package. It must not run the oracle package branch itself.

Existing verifier types (`numeric_exact`, `json_exact`, `string_exact`, `trace_event`) continue to work in parallel. A spec-backed runtime that legitimately wants a simple `numeric_exact` check uses the existing verifier type; it does not need an oracle package to use the simple verifier path. The oracle package path is opt-in per task via `verifier_type="oracle_package"`.

### 13.6 Sealed evaluator payload schema

Define `SealedEvaluatorPayload` explicitly in `agintor/evaluation/oracle_runner.py`:

```python
class SealedEvaluatorPayload(BaseModel):
    package: OraclePackage  # sealed projection
    fixture_resolver_id: str  # callable registered by ID, not inlined
    trace_events: list[TraceEvent] = Field(default_factory=list)
    side_effect_receipts: list[SideEffectReceipt] = Field(default_factory=list)
    workspace_root: str = ""
    sealed_artifact_refs: dict[str, str] = Field(default_factory=dict)
```

Constructor lives in `RuntimeEvaluator._build_sealed_payload(run, package)`. Fixtures are referenced by ID (resolved by an evaluator-side registry), not inlined into the payload, to avoid serializing large sealed artifacts.

### 13.7 LangGraph API subset

Pass 1 uses only:

- `StateGraph(state_schema)` — `state_schema` is `dict` in pass 1 (typed schema is pass 2 cleanup).
- `add_node(node_id, callable)`.
- `set_entry_point(node_id)`.
- `add_edge(source, target)`.
- `set_finish_point(terminal_id)`.
- `compile()`.
- `invoke(initial_state)`.

**Not** used in pass 1:

- Conditional edges (`add_conditional_edges`).
- Parallel/fan-out branches.
- `interrupt` / `resume`.
- Subgraphs.
- Checkpointers (the LangGraph-native ones).

Document this constraint in the solve-time executor module docstring. If a spec requires features outside this subset, the executor/compiler fails fast with a clear error in pass 1 and the feature is added to the deferred ledger.

### 13.8 Provider precedence

The `--provider` CLI flag overrides the spec's `models[].provider_name` at the `RuntimeOperationService` provider-call site. Implementation: `RuntimeOperationService.__init__` accepts an optional `provider_override: ModelProvider | None` parameter; when set, all model calls route to the override regardless of spec policy.

Spec `models[].provider_name` becomes the *default* used when no override is provided. Tested by `tests/test_provider_precedence_langgraph.py`.

### 13.9 OpenAITraceContext propagation

`OpenAITraceContext` is provider-agnostic correlation metadata (per AGENTS.md). The LangGraph compiler must propagate it through the spec runtime so traces correlate across host/runtime/oracle.

- Add `trace_context: OpenAITraceContext | None` to `LangGraphRuntimeState`.
- `RuntimeOperationService` reads it from state and attaches it to every provider call.
- `OracleEvaluationRunner` reads it from the run result and attaches it to validator-emitted trace events.

### 13.10 Runtime kind pinning through factory chat state

`runtime_kind` is chosen at build time from the CLI/profile/goal constraints and must persist through `GoalSpec.constraints`, `RuntimePlan`, build/export summaries, and factory chat identity/message state. Follow-up messages in the same factory chat continue the existing runtime kind. If a follow-up tries to switch from `policy_modules` to `langgraph_spec` (or the reverse), reject it with a clear "start a new factory chat" error instead of silently rebuilding the project under a different runtime substrate.

---

## 14. Historical verification checklist (Definition of Done)

Pass 1's historical completion standard was the checklist below. Its checkbox state records only what the original slice reran and must not be interpreted as the current backlog.

1. ☐ New runtimes can be represented as `RuntimeSpec` with stable digesting (`test_runtime_spec`, `test_runtime_spec_digest_stability`) — open: not rerun in this pass-1 hygiene slice.
2. ☐ `SpecAction` mutates spec-backed runtimes and writes a mutation ledger (`test_spec_actions`, `test_spec_mutator`) — open: existing coverage not rerun here.
3. ☐ New runtimes compile into a runnable LangGraph app via `RuntimeSpecCompiler.compile_to_directory` (`test_langgraph_runtime_compiler`) — open: covered indirectly by new tests, but the named compiler test was not rerun.
4. ☐ spec-backed runtimes run through the existing host/runtime protocol (`test_runtime_host`, `test_runtime_execution`) — open: host/runtime protocol suites were outside this prompt's narrow run scope.
5. ☐ Optional LangGraph dependency is used when installed; deterministic fallback works when absent (`test_langgraph_runtime_compiler` with/without extra) — open: no with/without optional-dependency matrix was run here.
6. ☐ `OraclePackage` is created from `GoalSpec` and frozen before evaluation (`test_oracle_package`) — open: package creation was smoked, but the named package test was not rerun.
7. ☐ Public and sealed projections are separate and tested (`test_oracle_public_projection`, `test_oracle_sealed_eval`) — open: public inspect was smoked, but sealed projection tests were not rerun.
8. ☑ Candidate runtimes never see sealed/private validation material (`test_export_no_sealed_material` + manual `inspect-oracle --public`) — proven by `test_export_no_sealed_material_copies_only_public_oracle_projection` and CLI public/default smoke.
9. ☐ Evaluator runs validators and records `ValidatorResult` / `ClaimResult` (`test_oracle_evaluation_runner`) — open: evaluator validator recording was outside this hygiene slice.
10. ☐ Evidence records carry package hash, validator IDs, claim IDs, runtime spec digest, and evidence digest (Phase E acceptance — inspect a `.agintor_runs` row) — open: no `.agintor_runs` evidence row was inspected here.
11. ☐ Parent/child comparison requires the same oracle package hash (`test_progress_oracle_oracle_hash_block`) — open: existing progress-oracle coverage not rerun here.
12. ☐ `ProgressOracle` remains the only promotion gate (existing `test_progress_oracle` still green, no LLM calls inside `progress_oracle.py`) — open: broad progress-oracle suite was outside the requested narrow run scope.
13. ☐ Search/archive update only when promotion decision allows it (Phase F edits include this — verify the archive row schema) — open: archive row schema was not audited in this pass.
14. ☐ TradingAgents exists as one profile/family, not the generic oracle (`test_tradingagents_adapter`, `test_trading_oracle_package`) — open: TradingAgents coverage was not rerun here.
15. ☐ Existing policy-module runtime behavior still works (`test_runtime_host`, `test_runtime_execution`, `test_progress_oracle`, all policy-module search tests pass) — open: policy-module regression suites were intentionally not run.
16. ☐ No ignored/generated `.pyc`, `__pycache__`, or sealed oracle artifacts are committed (`git status` is clean of these after Phase G) — open: git hygiene is still being cleaned and verified in this pass.
17. ☑ spec-backed export succeeds in a clean worktree (`test_langgraph_export_clean_worktree`) — proven by `test_langgraph_export_clean_worktree_uses_tracked_runtime_profile_default`.
18. ☐ Solve-time runtime modules do not import factory/search/evaluation/oracle code (`test_runtime_langgraph_solve_time_imports`) — open: import-boundary test was not rerun here.
19. ☑ spec-backed evolve loop integration test passes (`test_langgraph_evolve_loop_integration`) — proven by `test_langgraph_evolve_loop_integration_runs_spec_mutator_and_runtime_pair_path`.
20. ☐ Provider override precedence holds for spec-backed runtimes (`test_provider_precedence_langgraph`) — open: provider-precedence coverage was not rerun here.
21. ☐ spec-backed runtimes report checkpoint/resume unsupported and host resume fails clearly (`test_langgraph_resume_capability`) — open: resume capability coverage was not rerun here.
22. ☑ Factory chat follow-ups cannot silently switch runtime kind (`test_factory_runtime_kind_pinning`) — proven by `test_factory_runtime_kind_pinning_reuses_kind_for_followups_and_rejects_switches`.

---

## 15. Historical CLI surfaces after pass 1

This block records the intended pass-1 surface; current command syntax is documented in `AGENTS.md` and the live Typer CLI.

```bash
agintor init-runtime <dir> --runtime-kind policy_modules   # current default
agintor init-runtime <dir> --runtime-kind langgraph_spec
agintor build-runtime "<goal>" --destination <dir> --runtime-kind langgraph_spec --steps 10
agintor solve <runtime_dir> --prompt "..."                    # works on policy-module and spec-backed runtimes
agintor solve <runtime_dir> <task_id> --suite demo
agintor eval <runtime_dir> --suite demo --oracle-package <pkg_dir>
agintor evolve <runtime_dir> --steps 10 --mutator heuristic-spec
agintor compile-oracle "<goal>" <pkg_dir> --runtime-kind langgraph_spec
agintor oracle-qa <pkg_dir>
agintor inspect-oracle <pkg_dir>                              # public-safe default
agintor inspect-oracle <pkg_dir> --sealed                     # rejected; sealed projection is evaluator-only
```

`inspect-runtime` and `diff-runtime` from source plan §13.2 are out of scope for pass 1 (§16).

Existing flags (`--provider`, `--runtime-backend`, `--api-key-file`, `--profile`, `--artifact-mode`, `--workspace`) continue to work on policy-module and spec-backed runtimes.

---

## 16. Historical out-of-scope deferrals

These items were deferred during pass 1. Current unresolved status belongs in `Dev Docs/DEFERRED_ISSUES_LEDGER.md` or a current plan, not in this historical list.

### Functionality deferrals

- LLM-led `compiler_graph` real wiring — only the deterministic-via-proposal hook lands now (§7.3).
- `OracleCompiler` adaptive subagent loop with goal/domain/benchmark/validator authors as live LangGraph nodes.
- `inspect-runtime` / `diff-runtime` / `diff-oracle` CLIs.
- Mermaid export.
- LangSmith tracing wiring.
- WS3 changes — embedding LangGraph state into `CheckpointEnvelope` requires a separate WS3 plan; until then `CompiledSpecRuntime.invoke` is non-resumable (§13.2).
- Inspect-AI runner, OpenAI Evals runner — the family files exist with stub `_run` bodies; live integration is pass 2.
- LangGraph features beyond the §13.7 subset (conditional edges, parallel branches, interrupt/resume, subgraphs).
- LangChain `create_agent` wiring inside `RuntimeOperationService` `agent` node — pass 1 uses a deterministic agent loop; LangChain integration is pass 2.
- Typed Pydantic `StateGraph` state schema (pass 1 uses `dict`; pass 2 cleanup).

### WS interaction deferrals (cross-WS items)

- WS2 paused-run reduction interaction with spec-backed runtime resume (existing entry in `DEFERRED_ISSUES_LEDGER.md`). spec-backed is non-resumable in pass 1, so no immediate conflict, but flag for WS3 work.
- WS2 ReplayProvider branch-clone determinism — does not directly affect spec-backed path, but `provider_override` in §13.8 may need parallel review.
- WS5 tool/provider/control changes that may interact with `RuntimeOperationService` dispatch (§5).

---

## 17. Historical risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Stack's `OracleCompiler` hardcodes domain trigger words ("trading", "repo", etc.). New domains require editing the compiler core. | High | Medium | §7.3 keeps the deterministic body but moves trigger detection behind `family.score_applicability(context)`. Before adding a new domain, register a family with applicability and remove the keyword check from the compiler. |
| LangGraph API drift. | Medium | High | `_build_langgraph_app` is the single import site. Wrap in `try/except` and treat any import error as "fall back to sequential". §13.7 fixes the API subset. Add a smoke test in CI that imports `langgraph.graph.StateGraph` and skips with a clear message if missing. |
| The four-layer architecture boundary erodes if `OracleCompiler` calls into `agintor/runtime/kernel/` internals. | Medium | High | Current enforcement is consolidated in `tests/test_import_boundaries.py`. |
| Comparison-block rule (§8.5) breaks existing policy-module promotion flow because old comparisons have empty `oracle_package_hash`. | Medium | High | The block fires only when **both** sides have a non-empty hash and they differ, or when one side has a hash and the other does not (policy-module↔spec-backed boundary). policy-module→policy-module comparisons have empty hashes on both sides and pass through. Cover this with three test cases in `test_progress_oracle_oracle_hash_block.py`. |
| Stack's existing-edit anchors drift if the live repo evolves before this lands. | Medium | Medium | Phase C/E/F/G/H land within the same workstream sprint. If anchors drift, re-derive the SEARCH block from the current file content; do not blindly retry the diff. |
| Clean-worktree spec-backed export could fail because `agintor/templates/baseline_runtime/runtime_profile.json` is ignored. | High | High | Resolved by the tracked default at `agintor/runtime/sdk/defaults/runtime_profile.json`; retained clean-worktree tests guard it. |
| Sealed evaluator payload contract evolves and breaks `OracleEvaluationRunner` callers. | Low | Medium | §13.6 defines `SealedEvaluatorPayload` as a Pydantic model with schema-compatible field defaults. All new fields land with defaults. |
| `--provider` override semantics differ across policy-module/spec-backed runtimes. | Medium | Medium | §13.8 explicitly defines precedence. `test_provider_precedence_langgraph.py` covers spec-backed; existing tests cover policy-module. |
| Provider hook in `OracleCompiler` is stub-only in pass 1, hides design issues that only surface in pass 2. | Low | Medium | §7.3 keeps the proposal pattern as the integration shape so pass 2 doesn't have to refactor compiler internals — just wire `compiler_graph.run` to real subagents. |
| Integration test `test_langgraph_evolve_loop_integration` is slow or flaky. | Medium | Low | Mark `@pytest.mark.integration`. Run in the integration slice, not the per-phase slices. Use `--basetemp` to keep workspace local on Windows. |

---

## 18. Historical TradingAgents evidence target

Pass 1 required the `trading_outcome` validator family to cover the following claims for trading goals. The historical implementation used an optional local checkout at upstream commit `a5cb7cb`; that checkout is not tracked or required, and current authority is `agintor/integrations/tradingagents/`.

1. **Data cutoff integrity.** Decisions were made with data available only before the decision cutoff. Validator inspects trace + data-snapshot ledger for any post-cutoff reads.
2. **Order validity.** Recommendations map to valid bounded order intents (instrument, side, quantity, price bounds, time-in-force).
3. **Fill reconciliation.** Fills reconcile with orders — no fills without an originating order, no orphan orders without resolution.
4. **Portfolio reconciliation.** Portfolio state reconciles with fills, cash, costs, and positions. No state drift between rebalance windows.
5. **Cost / slippage.** Costs and slippage are computed and bounded per the spec's `cost_policy`.
6. **Risk policy compliance.** Risk constraints (position size, concentration, leverage, drawdown) are obeyed. Violations are hard-fail claims.
7. **Post-close outcome snapshot.** EOD scoring uses frozen price snapshots. Outcome metrics (net PnL, alpha, drawdown, risk-adjusted return) are computed consistently across runs.
8. **Runtime identity match.** Runtime identity and spec digest match the evaluated candidate (no swap-mid-run).

These claims do not appear for non-trading goals. `test_trading_oracle_package` covers presence on trading goals; `test_tradingagents_adapter` covers absence on non-trading goals.

External TradingAgents dependency must remain optional — install via `pip install -e ".[tradingagents]"` (add to `pyproject.toml` optional-dependencies in Phase I).

---

## 19. Glossary

- **Stack** — the bundle at `agintor_full_oracle_langgraph_stack/`.
- **Patch** — the bundle at `agintor_full_plan_patch/`.
- **`RuntimeSpec`** — the spec-backed typed runtime genome. JSON-canonicalized, hashable, mutated through `SpecAction`s.
- **`OraclePackage`** — frozen validation artifact for one `GoalSpec` + `RuntimeSpec`. Has public and sealed projections.
- **`OracleEvaluationRunner`** — runs each `ValidatorSpec` against a `RunResult`, produces `ValidatorResult`s and `ClaimResult`s.
- **`SpecActionMutator`** — replaces `HeuristicPatchMutator` for spec-backed runtimes. Emits typed `SpecAction`s; does not edit Python policy files.
- **`ValidatorFamily`** — runnable validator family conforming to the §3 interface.
- **Authority ceiling (A0–A5)** — `AuthorityLevel` enum from `agintor/contracts/evidence.py`. A0 = none, A5 = human-audited.
- **Solve-time module** — code shipped inside exported runtimes and the bundled runtime kernel. Cannot import factory/search/evaluation/oracle code (§6).
- **Factory-time module** — code that lives on the factory side only. May call into runtime/oracle helpers; not shipped with exported runtimes.
- **Pass 1** — the slice that ends at the §14 Definition of Done. Pass 2 covers the deferrals in §16.

---

## 20. Reference index

- Source design plan: [`LangGraph and Oracle Refactor Plan pass 1.md`](Archive%20Only%20-%20Zero%20Authority/LangGraph%20and%20Oracle%20Refactor%20Plan%20pass%201.md).
- Historical synthesis plan: [`LANGGRAPH_ORACLE_SYNTHESIS_PLAN.md`](Archive%20Only%20-%20Zero%20Authority/LANGGRAPH_ORACLE_SYNTHESIS_PLAN.md).
- Historical corrected plan: [`ORACLE_LANGGRAPH_REFACTOR_IMPLEMENTATION_PLAN.md`](Archive%20Only%20-%20Zero%20Authority/ORACLE_LANGGRAPH_REFACTOR_IMPLEMENTATION_PLAN.md).
- Stack bundle: removed after synthesis; no live `agintor/` or `tests/` dependency.
- Patch bundle: removed after synthesis; no live `agintor/` or `tests/` dependency.
- Architectural rules: [`AGENTS.md`](../AGENTS.md).
- Deferred issue tracker: [`Dev Docs/DEFERRED_ISSUES_LEDGER.md`](DEFERRED_ISSUES_LEDGER.md) — current ledger for real, non-urgent issues that remain open.

---

## Appendix A: Historical Phase 0 prep

At the time of pass 1, the implementer read:

1. `AGENTS.md` — architectural rules. Note the four-layer boundary, single `RUNTIME_CONTRACT_VERSION`, no-fallback rule, deferred-ledger discipline.
2. The WS4, WS2, and WS5 workstream plans now retained in `Dev Docs/Archive Only - Zero Authority/`.
3. `Dev Docs/Archive Only - Zero Authority/LangGraph and Oracle Refactor Plan pass 1.md` — source design plan.
4. `Dev Docs/DEFERRED_ISSUES_LEDGER.md` — deferred items that affected the pass.
5. Historical bundle intent; the raw synthesis bundles are no longer live repo material.
6. This document, then used as the execution plan.

The original preflight checked repository state, compiled the package, and ran the then-current focused tests. Those old test paths were later consolidated; use `AGENTS.md` and the live test tree for any present-day validation choice.

---

## Appendix B: Things to NOT take from either bundle (consolidated)

Never copy:

```text
agintor_full_oracle_langgraph_stack/**/__pycache__/
agintor_full_oracle_langgraph_stack/**/*.pyc
agintor_full_oracle_langgraph_stack/apply_search_replace.py     # wholesale apply
agintor_full_plan_patch/write_patch_files*.py                   # emission scripts
agintor_full_plan_patch/EXISTING_FILE_EDITS.search_replace.md   # shallower than Stack's
agintor_full_plan_patch/new_files/agintor/oracle/projections.py # leaks sealed metadata
agintor_full_plan_patch/new_files/agintor/oracle/families/*.py  # build_specs-only, inert
repo-root templates/baseline_runtime_langgraph/                        # wrong location
sealed oracle package files into exported runtimes              # invariant 5
```

Manual rebase order (matches §12):

1. Contracts (Phase A new files + Phase B __init__).
2. Oracle package IO/projection/QA.
3. Deterministic compiler and runnable validators.
4. Evaluator evidence integration.
5. Runtime spec-backed host execution.
6. Search/archive.
7. Factory export / CLI.
8. TradingAgents.
9. pyproject extras.

Do not start with the runtime migration alone. The runtime substrate and oracle substrate land as a narrow vertical slice because the validation signal is the central product problem.

---

## Appendix C: Historical decision recap for the implementer

During the original implementation, the decision recap was:

- **"Should I take this file from Stack or Patch?"** → §1 table.
- **"This diff doesn't apply cleanly, what do I do?"** → re-derive the SEARCH block from the current file content (§17 risk row 5). Never blindly retry.
- **"Should I copy `__pycache__`?"** → No (§7.6).
- **"Should I add runtime/schema marker fields or numbered runtime names?"** → No (§9, §13.1). Use plain runtime kinds and the existing `RUNTIME_CONTRACT_VERSION`.
- **"Should I swallow this validator exception?"** → No (§3, §9). Convert to `ValidatorResult(status="error")`.
- **"Should I add a conditional edge or parallel branch in LangGraph?"** → No in pass 1 (§13.7). File a deferred-ledger item.
- **"Should this oracle compiler call into solve-time runtime internals?"** → No (§2.1). Use `RuntimeHost`, `agintor/runtime/api/`, or the protocol entrypoint.
- **"Should this exported runtime contain `oracle/sealed.json`?"** → No (§2.5).
- **"Should I block a policy-module→policy-module comparison because of the new hash rule?"** → No, only when both have hashes and they differ, or when one has a hash and the other doesn't (§13.4).
- **"Should I write a parallel planning/analysis doc to remember this pass-1 decision?"** → No. At the time, this plan was edited in place.
- **"Should I commit `agintor/templates/baseline_runtime/runtime_profile.json`?"** → No. Option 1 from §7.9 landed at `agintor/runtime/sdk/defaults/runtime_profile.json`.

End of plan.
