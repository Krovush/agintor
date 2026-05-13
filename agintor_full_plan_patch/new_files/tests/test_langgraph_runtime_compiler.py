from __future__ import annotations

from agintor.contracts import default_langgraph_runtime_spec
from agintor.runtime.langgraph.compiler import LangGraphRuntimeCompiler


def test_langgraph_compiler_fallback_smoke_run():
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    state = LangGraphRuntimeCompiler().smoke_run(spec)
    assert "node.default" in state["completed_node_ids"]
