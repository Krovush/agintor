# Agintor full pass-1 refactor patch bundle

This bundle implements the pass-1 plan as a copy/apply patch package:

- `new_files/` contains every new file in full, one file per repo path.
- `EXISTING_FILE_EDITS.search_replace.md` contains SEARCH/REPLACE diffs for existing files.
- `APPLY_ORDER.md` gives the intended apply and test order.

The GitHub connector available in this chat can fetch repository content but does not expose a write/branch creation operation, so this bundle is provided as local artifacts rather than an applied PR.

## Implemented pass-1 spine

- RuntimeSpec v2 and SpecAction contracts
- OraclePackage contracts, hashing, public/sealed projections, QA
- Deterministic OracleCompiler scaffold and compiler graph shell
- Validator registry and initial validator families
- LangGraph runtime compiler with fallback sequential executor
- SpecActionMutator for typed runtime mutation
- TradingAgents adapter, spec profile, and outcome validator hooks
- Baseline v2 runtime template
- Focused tests for contracts, projections, QA, compiler fallback, and TradingAgents adapter
- Search/replace diffs to thread package/spec identity through contracts, evaluator, progress oracle, loader, search, and CLI
