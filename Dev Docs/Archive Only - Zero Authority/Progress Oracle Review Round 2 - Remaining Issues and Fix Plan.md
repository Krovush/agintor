# Progress Oracle — Round-2 Code Review and Fix Plan

**Status of prior review (12 items):** of the original 12 issues, only 4 were genuinely fixed; 4 remain unchanged; 3 were touched but the fix introduced a worse problem; 1 was pulled into a new design that creates a critical regression. Two new issues were introduced. Full pytest suite passes (324 tests, all green) — but the green tests do not exercise the engine end-to-end on a non-trivial suite, which is where the regression hides.

This document is keyed to the production search loop, not to the unit tests, because the unit tests are passing while the engine is structurally broken in production.

---

## Status of prior items

| # | Prior issue | Status | Notes |
|---|---|---|---|
| 1 | `EfficiencyDelta` import missing | **Fixed** | `agintor/contracts` re-export added at [agintor/evaluation/progress_oracle.py:11](agintor/evaluation/progress_oracle.py#L11). |
| 2 | `axis_id == task_id` mismatch with explicit contracts | **Not fixed** | See **R1** below. |
| 3 | `_aggregate_quality` LCB stand-in | **Replaced with a different stand-in** | See **R2**. |
| 4 | `mean_improvement` singleton semantics | **Fixed** | Now `singleton_margin=0.0` at [agintor/evaluation/scoring.py:141](agintor/evaluation/scoring.py#L141), test `test_singleton_mean_improvement_preserves_neutral_non_regression` pins the contract. |
| 5 | Stage-4 invalid suites lose their decision in the engine | **Fixed** | [agintor/search/engine.py:522-525](agintor/search/engine.py#L522-L525) now lifts `promotion_decision` before the `invalid` gate. Oracle also short-circuits `decide_evaluations` to `reject` when `child.invalid`. |
| 6 | `source = "frontier"` substring match on task_id | **Not fixed** | See **R3**. |
| 7 | Pairwise comparator is a placeholder | **Not fixed** | Unchanged; not wired into `decide()`. See **R4**. |
| 8 | `subskill`/`preference` declared but unproduced | **Half-fixed (badly)** | `subskill` is now produced — and produced for *every* engine-driven promotion, which creates the critical regression **C1**. `preference` still unproduced. |
| 9 | Defect-search and metamorphic comparators missing | **Not fixed** | See **R5**. |
| 10 | `_decision_attr`/`_decision_value` duplicated | **Not fixed** | Now duplicated across four files. See **R6**. |
| 11 | `load_suite` triple-aliasing | **Not fixed** | [agintor/evaluation/benchmarks.py:452](agintor/evaluation/benchmarks.py#L452) still aliases three names to one suite. |
| 12 | `ScoreCalculator` docstring | **Fixed** | [agintor/evaluation/scoring.py:27](agintor/evaluation/scoring.py#L27). |

---

## Critical regressions introduced by the round-2 fixes

### C1 — Capability promotion is unreachable in production (BLOCKER)

The oracle now refuses to emit `capability` whenever the contract is implicit:

```python
# agintor/evaluation/progress_oracle.py:289-304
if improved_axes and quality.lower > self.config.capability_epsilon:
    if _is_implicit_suite_contract(contract):
        return self._decision(comparison, "subskill", ...)
    return self._decision(comparison, "capability", ...)
```

`decide_evaluations` (the only entry point the engine calls via Stage 4) **always** builds an implicit contract:

```python
# agintor/evaluation/progress_oracle.py:170-185
def decide_evaluations(self, parent, child) -> PromotionDecision:
    comparison = self.compare_evaluations(parent, child)
    contract = self._implicit_contract(comparison)   # always implicit
    ...
    return self.decide(contract=contract, comparison=comparison, ...)
```

Reproduction (took 0.3s, no hand-built `PairedComparison` needed):

```
parent: 8 frontier tasks × 3 seeds, score 0.0 each
child:  same tasks × seeds, score 1.0 each
ProgressOracle().compare(parent, child).decision == "subskill"
```

So in production, every quality win is tagged `subskill`. **The `decide_evaluations` path can never produce `capability`.** The new test `test_implicit_suite_quality_win_promotes_subskill_not_capability` pins this as intended behavior, which makes the regression invisible to CI.

### C2 — `subskill` is a write-only archive (BLOCKER, follows from C1)

Every engine read path is hard-coded to `archive_kind="capability"`:

| Reader | Location |
|---|---|
| `_exemplars` | [agintor/search/engine.py:358](agintor/search/engine.py#L358) |
| `_validation_tick` (leader pick) | [agintor/search/engine.py:365](agintor/search/engine.py#L365) |
| `_maybe_crossover` (donor pool) | [agintor/search/engine.py:426](agintor/search/engine.py#L426) |
| `select_parent` (parent for next mutation) | [agintor/search/engine.py:491](agintor/search/engine.py#L491) |
| `best_train` (final summary) | [agintor/search/engine.py:624](agintor/search/engine.py#L624) |
| Export (`_export_candidate_records`) | [agintor/factory/export.py:171](agintor/factory/export.py#L171) — also pinned to capability |

`route_promotion_decision("subskill")` does insert into the subskill archive ([agintor/search/engine.py:96-103](agintor/search/engine.py#L96-L103)) — but no reader ever consults it. Combined with **C1**, the behavior of a real `agintor evolve` run is:

1. Seed the capability archive with the baseline.
2. For each step, mutate the *baseline* (the only capability member), evaluate, get `subskill`, file it in subskill, never re-read.
3. Because `select_parent("…", archive_kind="capability")` keeps returning the baseline, **the search re-derives from the baseline forever and accumulates no compounding gains.**

`CAPABILITY_CREDIT_DECISIONS = {"capability"}` ([agintor/search/engine.py:46](agintor/search/engine.py#L46)) makes the downstream worse:

- `accepted_scopes` ([line 382](agintor/search/engine.py#L382)) — empty in production, so phase-coverage is always 0.
- `pass_rate` ([line 391](agintor/search/engine.py#L391)) — always 0/N, so `maybe_advance_phase` never advances.
- Counterfactual probes ([line 554](agintor/search/engine.py#L554)) — never run.
- `accepted_since_retrain` ([line 460](agintor/search/engine.py#L460)) — never increments, so predictor retrain rarely fires.

**Tests miss this** because no test runs the actual evolution loop on a multi-step suite and asserts that a child becomes a parent later. This is the most important fix and should land before anything else.

### C3 — Predictor learning is starved

`_update_predictors` is only called when `route.predictor_family_prefix is not None` ([agintor/search/engine.py:550-551](agintor/search/engine.py#L550-L551)).

Per `route_promotion_decision`:
- `capability` → `"capability"` ✓ (but unreachable per C1)
- `efficiency` → `"efficiency"` ✓
- `subskill` → `None` ✗
- `preference` → `"preference"` ✓ (but unreachable; never produced)
- `reject`/`abstain`/`no_progress`/`quarantine` → `None` ✗

So in production, predictors only train on `efficiency` observations — none of the (overwhelming majority) `subskill` or `no_progress` runs feed back into the predictor at all. The previous code trained predictors on every full evaluation regardless of acceptance.

---

## Issues remaining from prior review

### R1 — `_quality_axis_deltas` still keys on `task_id`

[agintor/evaluation/progress_oracle.py:334-360](agintor/evaluation/progress_oracle.py#L334-L360) constructs one axis per task and sets `axis_id = task_id`. Real `DomainEvidenceContract` instances declare semantic axis ids (e.g. `"expression_generalization"`); `_axis_epsilon` and `_axis_regression_tolerance` look these up by id and silently fall back to defaults when no match is found.

The implicit contract in `_implicit_contract` masks this — it manufactures one axis spec per task_id from the comparison, so the lookup always succeeds. But that path is exactly the one stuck on `subskill` (C1). The actual purpose of explicit contracts (different epsilons per semantic axis) is unreachable when going through `compare_evaluations`.

### R2 — `_aggregate_quality` swapped one stand-in for another

```python
# agintor/evaluation/progress_oracle.py:487-497
return PairedEffect(
    estimate=mean(estimates),
    lower=max(-1.0, mean([axis.lower for axis in axis_deltas])),
    upper=min(1.0, mean([axis.upper for axis in axis_deltas])),
    n_eff=...,
)
```

Two problems:

1. **Unweighted mean** — an axis with `evidence_count=64` gets the same weight as one with `evidence_count=1`. `n_eff` is computed correctly, but never propagated into the bound.
2. **Mean of LCBs is not an LCB on the mean.** For independent axes the proper margin shrinks by `√n_axes`; this aggregation preserves the per-axis margin instead. It's safer than the prior `std_error(estimates)` approach (which under-counted within-axis variance) but is overly conservative when many axes contribute.

This is still not the empirical-Bernstein bound the spec calls for, just a different shape of stand-in.

### R3 — Frontier-source detection still substring-matches `task_id`

```python
# agintor/evaluation/progress_oracle.py:344
source = "frontier" if "frontier" in task_id or "generated_tool_workflow" in task_id else "static_exact"
```

The new generator hard-codes `task_id_prefix="tool.frontier"`, so the substring match works for the canonical case. Anyone parameterising the prefix (or any other domain that doesn't ship the literal "frontier" in its IDs) silently gets `source="static_exact"`, which feeds into the saturation gate at [line 315](agintor/evaluation/progress_oracle.py#L315) and produces wrong abstain reasons.

The challenge generator already writes `task.metadata["domain_kind"] == "generated_tool_workflow"`. The oracle should consume that, not the task_id.

### R4 — `PairwiseArtifactComparator` is still a stub and unused

[agintor/evaluation/pairwise_comparator.py](agintor/evaluation/pairwise_comparator.py) is unchanged from the prior round: simple `score_b - score_a`, no calibration, no order randomization beyond the test harness manually flipping arguments, no length-bias check, no authority level. Nothing in `decide()` calls it. The promised `pairwise_preference` axis type cannot be evaluated.

### R5 — No defect search, no metamorphic comparator

The challenge generator emits `metamorphic_tags` in `task.metadata` ([agintor/evaluation/challenge_generators.py:208](agintor/evaluation/challenge_generators.py#L208)) but no consumer reads it. There is no `DefectSearchComparator` (spec section "Defect search") and no metamorphic comparator (spec section "Metamorphic comparison"). For an MVP that's defensible — but the unsaturated-axis guarantee the spec requires (every promotable objective has at least one unsaturated quality axis) cannot be enforced without one of these.

### R6 — `_decision_attr` / `_decision_value` duplicated four times

| File | Lines |
|---|---|
| [agintor/search/engine.py](agintor/search/engine.py#L48-L66) | 4 helpers |
| [agintor/search/archive.py](agintor/search/archive.py#L240-L255) | 4 helpers |
| [agintor/learning/observations.py](agintor/learning/observations.py#L40-L54) | 2 helpers |
| [agintor/evaluation/progress_oracle.py](agintor/evaluation/progress_oracle.py#L65-L66) | 1 helper |

The shapes are slightly different (some accept `Mapping`, some don't; default value handling diverges). This is exactly the drift CLAUDE.md warns against.

### R7 — `load_suite` still triple-aliased

[agintor/evaluation/benchmarks.py:452](agintor/evaluation/benchmarks.py#L452) still resolves `tool-frontier`, `tool_frontier`, and `generated_tool_workflow_v1` to the same suite. Pick one canonical name.

---

## New issues introduced

### N1 — `_archive_objectives_for_promotion` only credits family/global for `capability`

```python
# agintor/search/engine.py:325-328
if decision_type == "capability":
    train_task_ids = {task.task_id for task in self.suite.train}
    if train_task_ids and train_task_ids.issubset(improved_axes):
        objectives.update(name for name in ("sbar:global", "rhobar:global") if name in available)
```

For `subskill` decisions (the only kind produced in production, per C1), `sbar:global` is never added. So even if subskill were readable, the global objective archive cell would never see a child. `best_train` ([line 624](agintor/search/engine.py#L624)) reads `sbar:global` from the capability archive and would always return `-inf` after the baseline.

### N2 — Health-floor gate inverted for the implicit contract

[agintor/evaluation/progress_oracle.py:526](agintor/evaluation/progress_oracle.py#L526) sets `health_floors={}` on the implicit contract, while `compare_evaluations` writes `leakage_status="unknown"` ([line 223](agintor/evaluation/progress_oracle.py#L223)).

`_leakage_issue` then checks:

```python
# agintor/evaluation/progress_oracle.py:147-149
requires_leakage_evidence = bool(contract.leakage_policy) or "leakage" in dict(contract.health_floors or {})
if requires_leakage_evidence and normalized not in {"clean", ...}:
    return "missing_leakage_evidence"
```

For the implicit contract, `leakage_policy={}` and `health_floors={}`, so `requires_leakage_evidence=False`, and `leakage_status="unknown"` passes. That's intentional but it means **every engine-driven decision is taken without a leakage check**, even though the new generator goes to the trouble of sealing answers via `private_expected`. The leakage policy needs to actually be set somewhere.

### N3 — `extract_predictor_observations(accepted=...)` is now dead weight

The `accepted` parameter survives only as a fallback when `decision is None`. When the engine calls through `_update_predictors`, it always passes a `promotion_decision`, so `accepted` is computed from `decision_type` inside the function and the parameter is redundant. Two signals for one thing — easy source of future confusion.

---

## Fix action plan

The plan is ordered by blast radius. Fix C1/C2 first; everything else is gravy until those land.

### Phase 1 — unblock the search loop (must land first)

**1. Decide whether the engine should produce `capability` or accept `subskill` as the promotion currency.**

Two viable options; pick one:

- **(A) Wire an explicit `DomainEvidenceContract` into Stage 4.** The factory pipeline knows which suite is in play (e.g. `tool-frontier`); a per-suite contract can be loaded at evaluator construction and threaded through `decide_evaluations`. Concretely:
  - Add `DomainEvidenceContract | None` parameter to `RuntimeEvaluator.__init__` (or load via suite name).
  - `decide_evaluations` falls back to the implicit contract only when none is supplied.
  - The frontier suite gets a real contract with semantic axis ids, a non-empty `leakage_policy`, and `minimum_frontier_tasks ≥ 32`.
- **(B) Treat `subskill` as a first-class evolution currency.**
  - Drop the `_is_implicit_suite_contract` branch in `decide`; emit `capability` directly when the LCB clears the threshold and `improved_axes` is non-empty.
  - Or, change every `archive_kind="capability"` reader in [engine.py](agintor/search/engine.py) and [factory/export.py](agintor/factory/export.py) to fall back to `subskill` when capability is empty.
  - Add `subskill` to `CAPABILITY_CREDIT_DECISIONS` and `CAPABILITY_COUNTERFACTUAL_DECISIONS` (rename to drop the misleading "capability" prefix while you're at it — `PROGRESS_CREDIT_DECISIONS`).

(A) is the WS4 spec's intent; (B) is the smallest change that restores the search loop. Recommend (A) for the frontier suite + (B) as the safety net for any unspeced suite, so `agintor evolve --suite demo` keeps working without forcing an explicit contract on every workstream.

**2. Add an end-to-end regression test for the search loop.**

`tests/test_evolution_engine_search.py` should run `EvolutionEngine.run(steps=3)` against a deterministic stub provider and assert:
- the capability *or* subskill archive contains > 1 record at the end,
- `select_parent` returns a non-baseline runtime on at least one step,
- `pass_rate` > 0 across the run.

This is the test that would have caught C1/C2 in round 1.

**3. Restore predictor learning for non-promoted runs.**

In [`engine.py:550-551`](agintor/search/engine.py#L550-L551), call `_update_predictors` for every `stage4` outcome (not gated on `predictor_family_prefix`). The promotion route can still control *which* predictor families fire, but `fault`/`cost` observations should always update so the optimizer learns from failures.

### Phase 2 — correctness of the oracle

**4. Stop substring-matching `task_id` for source detection (R3).**

`_quality_axis_deltas` should accept the suite tasks (or the `RunResult.task_metadata`, if we plumb that through) and read `task.metadata["domain_kind"]` / `task.metadata["slice_tags"]`. A run-result already includes the task_id; pass `evaluation.tasks` (or build a `task_id → metadata` map at the suite_score boundary) into `compare_evaluations`.

**5. Decouple `axis_id` from `task_id` (R1).**

For per-task axes, namespace them: `f"task:{task_id}"`. For domain-level axes, the explicit contract supplies the id and the oracle aggregates per-task evidence under that id. Concretely: contract declares `expression_generalization` with a tag matcher (`slice_tags ∋ "tool"`, or `domain_kind == "generated_tool_workflow"`), and the oracle bundles all matching task evidence into one axis_delta whose id is the contract axis. This makes per-axis epsilon/regression-tolerance work as intended.

**6. Weight `_aggregate_quality` by evidence_count (R2).**

```python
n = sum(max(0, axis.evidence_count) for axis in axis_deltas) or 1
estimate = sum(axis.estimate * axis.evidence_count for axis in axis_deltas) / n
# either weighted mean of axis.lower, or the proper SE-based bound
```

If there's appetite, follow the spec and put empirical-Bernstein on the per-pair deltas (the data is already paired in `_quality_axis_deltas`; the deltas list per axis is already there). That's a single helper in `progress_oracle` and a one-line swap in `_aggregate_quality`.

**7. Fix the leakage gate on the implicit contract (N2).**

Either:
- set a default `leakage_policy={"status_required": True}` on the implicit contract and have `compare_evaluations` write `leakage_status="clean"` after running the sealed-answer rescore, or
- have the implicit contract opt out of the leakage check explicitly with a dedicated reason code rather than silently passing.

The new sealed-answer logic in [evaluator.py:495-528](agintor/evaluation/evaluator.py#L495-L528) does protect against artifact leakage; the gate just needs to acknowledge that.

### Phase 3 — spec coverage and code quality

**8. Land a real pairwise comparator (R4) or remove the dead path.**

If you don't intend to ship pairwise comparison this cycle, delete [agintor/evaluation/pairwise_comparator.py](agintor/evaluation/pairwise_comparator.py) and its test, and strip `pairwise_preference` from the comparator-type literal in [agintor/contracts/evidence.py:113](agintor/contracts/evidence.py#L113). Otherwise: order randomization, `ComparatorCalibration`, length-bias detection, integration with `decide()`.

**9. Decide the fate of `preference` and `defect_search`/`metamorphic` (R5, prior #8/#9).**

Same triage: ship them or trim them from the contract literals. Half-built dead branches in `_optimizer_updates`, `route_promotion_decision`, and `archive_kind` keep accumulating maintenance cost.

**10. Consolidate `_decision_attr`/`_decision_value` (R6).**

Move the helpers to [agintor/contracts/evidence.py](agintor/contracts/evidence.py) (next to `_value`, which already exists) and import from the four call sites. Drop the divergent overloads.

**11. Drop two of the three `load_suite` aliases (R7).**

Keep `tool-frontier` (matches the suite `name` field). Remove `tool_frontier` and `generated_tool_workflow_v1` unless a caller depends on them — search the repo first.

**12. Drop `accepted` parameter from `extract_predictor_observations` (N3).**

The function should take only `decision`. Update the two call sites in [engine.py:451-455](agintor/search/engine.py#L451-L455) and [observations.py:204-217](agintor/learning/observations.py#L204-L217).

### Phase 4 — observability, after correctness

**13. Surface the promotion-type histogram in the build summary.**

`BuildSummary` already has the ledger paths. Add a `promotion_counts: dict[str, int]` (capability/subskill/efficiency/reject/abstain/no_progress) computed from `promotion_ledger.jsonl`. Without it the C1/C2 class of regression is invisible to anyone reading the summary.

**14. Add a sanity-check test that asserts non-zero capability rate on the frontier suite.**

After (1) lands: `tests/test_frontier_suite_promotes_capability.py` — runs evolve for ≥ 5 steps on `tool-frontier`, asserts the promotion ledger contains at least one `capability` decision. This is the end-to-end backstop for the spec invariant "no capability update from cost-only delta" *and* its dual "frontier wins must become capability promotions."

---

## Suggested landing order

| Step | Items | Rationale |
|---|---|---|
| PR 1 | C1/C2 (Phase 1.1, 1.2, 1.3) | Search loop is broken without these. Test gate: full evolve run produces ≥ 1 non-baseline parent on step ≥ 2. |
| PR 2 | R1/R3/N2 (Phase 2.4, 2.5, 2.7) | Oracle correctness and leakage gate. Builds on PR 1's wiring. |
| PR 3 | R2 (Phase 2.6) | Aggregation bound fix. Independent of the others, low risk. |
| PR 4 | R6/R7/N3 (Phase 3.10, 3.11, 3.12) | Cleanups; can be one tidy PR. |
| Defer | R4/R5/preference (Phase 3.8, 3.9) | Either cut or scope properly with their own work item; do not let them rot in-tree. Append to [DEFERRED_ISSUES_LEDGER.md](../DEFERRED_ISSUES_LEDGER.md) if cut. |
| PR 5 | Phase 4.13/4.14 | Observability — lands after the correctness fixes so the test is meaningful. |

---

## Risk notes

- **PR 1 will change the shape of `evolution_history.json` and `archive_index.json`** if it touches `archive_kind`. Per CLAUDE.md, MVP artifacts are disposable, so don't add migrations — but flag it in the PR description so anyone holding `.agintor_evo/` directories knows to wipe.
- **PR 2's source-detection change touches `RunResult` plumbing.** Confirm the runtime protocol does not strip `task.metadata["slice_tags"]` between host and runtime; if it does, the data needs to flow through `RunResult.task_metadata` instead.
- **None of the changes need backward-compat for older checkpoints** — see CLAUDE.md "Do not preserve backward compatibility for disposable MVP checkpoints."
