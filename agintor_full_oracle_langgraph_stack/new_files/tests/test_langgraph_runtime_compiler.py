from __future__ import annotations

from agintor.contracts import baseline_langgraph_runtime_spec
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler, compile_runtime_spec


def test_compile_runtime_spec_invokes_linear_graph():
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.invoke")
    app = compile_runtime_spec(spec)
    state = app.invoke("hello", request_id="r", task_id="t", seed=1)
    assert state.status == "completed"
    assert state.runtime_spec_digest == spec.spec_digest
    assert "answer" in state.artifacts


def test_runtime_spec_compiler_writes_runtime_files(tmp_path):
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.write")
    RuntimeSpecCompiler().compile_to_directory(spec, tmp_path / "runtime", force=True)
    assert (tmp_path / "runtime" / "runtime_spec.json").exists()
    assert (tmp_path / "runtime" / "runtime_manifest.json").exists()
    assert (tmp_path / "runtime" / "generated_langgraph_app.py").exists()
