# Agintor full Oracle / Evaluator / LangGraph stack patch bundle

This bundle implements the original LangGraph + adaptive OracleCompiler plan as a full patch pack.
It keeps the existing policy-file runtime path intact and adds a parallel `langgraph_spec_v2` path.

## What is included

- `RuntimeSpec` v2 durable runtime genome and stable digesting.
- `SpecAction` typed runtime mutations and mutation ledger support.
- `OraclePackage` with public/sealed projections, frozen hashing, QA reports, claim graph, validator specs, and authority policies.
- Deterministic `OracleCompiler` plus a LangGraph-compatible compiler workflow fallback.
- Validator family registry with exact, schema, repo-patch, stateful-service, trace-state, factual, pairwise, trading, human-audit, Inspect, OpenAI Eval, and consent-proof families.
- Oracle evaluation runner that emits `ValidatorResult` and `ClaimResult` records.
- Spec-backed LangGraph runtime compiler and compatibility shims for the current runtime loader shape.
- `SpecActionMutator` for v2 search evolution.
- TradingAgents adapter as a runtime-spec profile and validator family example, without hardcoding finance into the root oracle.
- Existing-file search/replace diffs for contracts, evaluator/progress, loader, search, CLI, export, planning, and dependencies.
- Focused tests for spec digests, public/sealed projections, compiler, QA, spec actions, runtime compiler, mutation, and TradingAgents.

## Applying

From the extracted bundle directory:

```bash
python apply_search_replace.py /path/to/agintor
```

This copies `new_files/` into the repo and applies all files under `existing_edits/` as exact search/replace diffs.

## Suggested validation

```bash
python -m compileall -q agintor
python -m pytest tests/test_runtime_spec.py tests/test_spec_actions.py tests/test_oracle_package.py tests/test_oracle_public_projection.py tests/test_oracle_sealed_eval.py
python -m pytest tests/test_langgraph_runtime_compiler.py tests/test_spec_mutator.py tests/test_tradingagents_adapter.py tests/test_trading_oracle_package.py
```

The LangGraph dependency is optional for the deterministic fallback path. Installing `agintor[langgraph]` enables the actual LangGraph compiler graph adapter.

## Important boundaries

- The exported runtime includes public validation summaries, runtime spec, and generated app code.
- The exported runtime must not include the sealed oracle package, private fixtures, hidden tests, private rubrics, or promotion thresholds.
- `ProgressOracle` remains the promotion authority; the adaptive compiler creates frozen packages but does not decide promotions.
- New oracle packages are diagnostic until parent and child are evaluated under the same package hash.
