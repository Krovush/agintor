from __future__ import annotations

from pathlib import Path

from agintor.benchmarks import build_demo_suite
from agintor.evolution import EvolutionEngine
from agintor.providers import LocalDeterministicProvider
from agintor.project import init_runtime



def test_evolution_engine_runs_smoke(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(build_demo_suite(), tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    summary = engine.run(steps=2)
    assert summary.archive_cells > 0
    assert summary.steps == 2
    assert len(engine.history) == 2
