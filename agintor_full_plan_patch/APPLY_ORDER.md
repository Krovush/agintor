# Apply order

1. Copy `new_files/` into the repository root.
2. Apply `EXISTING_FILE_EDITS.search_replace.md` in order.
3. Run:

```bash
python -m compileall -q agintor
python -m pytest tests/test_runtime_spec.py tests/test_spec_actions.py tests/test_oracle_package.py tests/test_oracle_public_projection.py tests/test_oracle_qa.py tests/test_langgraph_runtime_compiler.py tests/test_tradingagents_adapter.py
```

This package is intentionally pass-1/full-spine code: it adds typed contracts and adapters, preserves the existing runtime/evaluator/progress authority path, and avoids making LangGraph a hard dependency by using a sequential fallback compiler.
