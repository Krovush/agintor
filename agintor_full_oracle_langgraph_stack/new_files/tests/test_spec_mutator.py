from __future__ import annotations

from agintor.contracts import baseline_langgraph_runtime_spec
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.search.spec_mutator import HeuristicSpecActionMutator, SpecMutationContext


def test_spec_mutator_writes_child_and_ledger(tmp_path):
    parent = tmp_path / "parent"
    RuntimeSpecCompiler().compile_to_directory(baseline_langgraph_runtime_spec(runtime_id="mut.parent"), parent, force=True)
    candidate = HeuristicSpecActionMutator().mutate(
        SpecMutationContext(objective="sbar:global", touched_scope=["top"], runtime_dir=parent, workspace=tmp_path / "children", seed=1)
    )
    assert candidate.child_runtime_dir.exists()
    assert candidate.actions
    assert candidate.child_spec_digest != candidate.parent_spec_digest
    assert (candidate.child_runtime_dir / "mutation_ledger.jsonl").exists()
