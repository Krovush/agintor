from __future__ import annotations

from pathlib import Path

from agintor.memory_graph import LongTermGraph, ShortTermGraph
from agintor.project import baseline_template_dir, init_runtime
from agintor.prompt_builder import METHOD_CONTRACTS
from agintor.runtime_api import RuntimeBudget
from agintor.runtime_loader import load_runtime


def test_runtime_template_path_resolves_and_initializes(tmp_path: Path) -> None:
    template_dir = baseline_template_dir()
    assert template_dir.exists()
    assert (template_dir / "runtime_manifest.json").exists()
    runtime_dir = init_runtime(tmp_path / "runtime")
    assert runtime_dir.exists()
    assert (runtime_dir / "runtime_manifest.json").exists()


def test_runtime_manifest_matches_four_mutable_interfaces_and_shell_boundary(tmp_path: Path) -> None:
    runtime = load_runtime(init_runtime(tmp_path / "runtime"))
    policy_files = {
        scope: module_ref.split(":", 1)[0]
        for scope, module_ref in runtime.manifest.policy_modules.items()
    }
    assert set(policy_files) == {"top", "mem", "tool", "ctl"}
    assert set(runtime.manifest.mutable_files) == set(policy_files.values())
    assert set(runtime.manifest.mutable_files).isdisjoint(set(runtime.manifest.immutable_manifest))


def test_baseline_runtime_exposes_all_mutable_methods_from_spec(tmp_path: Path) -> None:
    runtime = load_runtime(init_runtime(tmp_path / "runtime"))
    policy_objects = {
        "top": runtime.topology,
        "mem": runtime.memory,
        "tool": runtime.tooling,
        "ctl": runtime.control,
    }
    for scope, methods in METHOD_CONTRACTS.items():
        missing = [method for method in methods if not hasattr(policy_objects[scope], method)]
        assert missing == []


def test_runtime_budget_and_graph_contracts_match_spec_defaults() -> None:
    budget = RuntimeBudget(cost=50.0, latency=60.0, calls=32, checks=8, C_max=100.0, L_max=120.0, M_max=64, Q_max=16)
    assert budget.normalized() == {"cost": 0.5, "latency": 0.5, "calls": 0.5, "checks": 0.5}
    assert budget.exhausted() is False
    budget.checks = 16
    assert budget.exhausted() is True
    assert ShortTermGraph.REQUIRED_NODE_TYPES == {"AgentRun", "Event", "Summary", "Artifact", "RawBlob", "OpenHandle", "VerifierEvidence"}
    assert ShortTermGraph.REQUIRED_EDGE_TYPES == {"CALLS_AGENT", "EMITS", "SUMMARIZES", "PRODUCES", "BACKLINKS_TO", "WAITS_ON", "CONTINUES_FROM", "VALIDATED_BY"}
    assert LongTermGraph.REQUIRED_TYPES == {"Symbol", "File", "Query", "Answer", "ToolFailure", "FixPattern", "TaskNote", "Procedure", "EnvironmentFingerprint", "ArtifactSignature"}
