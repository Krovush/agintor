# Baseline Runtime v2 Template

This template is generated from `baseline_langgraph_runtime_spec()` by `RuntimeSpecCompiler`.
It is intentionally spec-first: `runtime_spec.json` is the mutable genome and the generated
LangGraph/LangChain app is a compilation artifact.

Use:

```python
from agintor.contracts import baseline_langgraph_runtime_spec
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
RuntimeSpecCompiler().compile_to_directory(baseline_langgraph_runtime_spec(), "runtime-v2", force=True)
```
