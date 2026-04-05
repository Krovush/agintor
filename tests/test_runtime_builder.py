from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import agintor.runtime_builder as runtime_builder
from agintor.exceptions import RuntimeLoadError
from agintor.goal_rubric import build_goal_spec
from agintor.project import init_runtime as init_runtime_dir
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_builder import build_goal_conditioned_suite, build_runtime_from_goal
from agintor.runtime_loader import (
    RUNTIME_ABI_VERSION,
    RUNTIME_EXPORT_BUNDLE_FILE,
    RUNTIME_PROVENANCE_BUNDLE_FILE,
    load_runtime,
)
from agintor.runtime_profile import default_runtime_profile
from agintor.schemas import ArchiveEntry, ArchiveRecord, SuiteEvaluation

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")

def _candidate_record(runtime_dir: Path, runtime_hash: str, scores: dict[str, float], *, objective: str = "sbar:global") -> ArchiveRecord:
    return ArchiveRecord(
        objective=objective,
        key=runtime_hash,
        entry=ArchiveEntry(
            code_hash=f"code-{runtime_hash}",
            runtime_hash=runtime_hash,
            scores=scores,
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="seed",
            complexity_bucket=0,
            mutable_loc=1,
            trace_refs=[],
        ),
        runtime_dir=str(runtime_dir),
    )


def _write_runtime_dir(path: Path, marker: str) -> Path:
    init_runtime_dir(path, force=True)
    (path / "marker.txt").write_text(marker, encoding="utf-8")
    return path


def _goal_score_keys(goal_prompt: str) -> list[str]:
    suite = build_goal_conditioned_suite(goal_prompt, default_runtime_profile())
    return [
        f"s:{task.task_id}"
        for task in suite.train
        if task.metadata.get("goal_conditioned") is True
    ]


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates_by_objective: dict[str, list[ArchiveRecord]],
    validation_scores: dict[str, float],
    validation_calls: list[Path],
    init_calls: list[dict[str, object]] | None = None,
    archive_setup=None,
    patch_init_runtime: bool = True,
) -> None:
    def fake_init_runtime(destination: str | Path, force: bool = False) -> Path:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        return path

    class FakeEvaluator:
        def evaluate_validation(self, runtime_path: Path) -> SuiteEvaluation:
            validation_calls.append(Path(runtime_path))
            return SuiteEvaluation(
                runtime_hash=Path(runtime_path).name,
                objective_scores={"sbar:global": validation_scores[str(runtime_path)]},
                task_scores={},
                family_scores={},
                run_results=[],
                invalid=False,
            )

    class FakeArchive:
        def __init__(self) -> None:
            self.runtime_dirs: dict[str, str] = {}
            self.runtime_evaluations: dict[str, SuiteEvaluation] = {}
            self.runtime_descriptors: dict[str, SimpleNamespace] = {}
            for records in candidates_by_objective.values():
                for record in records:
                    runtime_hash = record.entry.runtime_hash
                    self.runtime_dirs[runtime_hash] = record.runtime_dir
                    self.runtime_evaluations[runtime_hash] = SuiteEvaluation(
                        runtime_hash=runtime_hash,
                        objective_scores=dict(record.entry.scores),
                        task_scores={},
                        family_scores={},
                        run_results=[],
                        invalid=False,
                    )
                    self.runtime_descriptors[runtime_hash] = SimpleNamespace(
                        code_hash=record.entry.code_hash,
                        behavior_bin=list(record.entry.behavior_bin),
                        scope_tag=record.entry.scope_tag,
                        complexity_bucket=record.entry.complexity_bucket,
                        mutable_loc=record.entry.mutable_loc,
                    )

        def island(self, objective_name: str) -> list[ArchiveRecord]:
            return list(candidates_by_objective.get(objective_name, []))

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            if init_calls is not None:
                init_calls.append(dict(kwargs))
            self.workspace = Path(args[1]) if len(args) > 1 else tmp_path / "fake_evolution"
            self.archive = FakeArchive()
            if callable(archive_setup):
                archive_setup(self.archive)
            self.evaluator = FakeEvaluator()

        def run(self, steps: int = 10) -> SimpleNamespace:
            self.workspace.mkdir(parents=True, exist_ok=True)
            (self.workspace / "evolution_history.json").write_text("[]", encoding="utf-8")
            (self.workspace / "archive_index.json").write_text("[]", encoding="utf-8")
            (self.workspace / "validation_history.json").write_text("[]", encoding="utf-8")
            (self.workspace / "stage_failures.json").write_text("[]", encoding="utf-8")
            return SimpleNamespace(
                best_train_score=0.42,
                archive_cells=sum(len(records) for records in candidates_by_objective.values()),
                accepted=2,
                history_path=str(self.workspace / "evolution_history.json"),
                archive_index_path=str(self.workspace / "archive_index.json"),
                validation_history_path=str(self.workspace / "validation_history.json"),
                stage_failures_path=str(self.workspace / "stage_failures.json"),
            )

    if patch_init_runtime:
        monkeypatch.setattr(runtime_builder, "init_runtime", fake_init_runtime)
    monkeypatch.setattr(runtime_builder, "EvolutionEngine", FakeEngine)


def test_build_runtime_exports_highest_goal_score_before_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    low_goal_high_val = _write_runtime_dir(tmp_path / "low_goal_high_val", "low-goal")
    high_goal_low_val = _write_runtime_dir(tmp_path / "high_goal_low_val", "high-goal")
    candidates = [
        _candidate_record(
            low_goal_high_val,
            "low-goal",
            {
                **{key: 0.55 for key in goal_score_keys},
                "sbar:global": 0.70,
            },
        ),
        _candidate_record(
            high_goal_low_val,
            "high-goal",
            {
                **{key: 0.80 for key in goal_score_keys},
                "sbar:global": 0.40,
            },
        ),
    ]
    validation_calls: list[Path] = []
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={"sbar:global": candidates},
        validation_scores={
            str(low_goal_high_val): 0.95,
            str(high_goal_low_val): 0.10,
        },
        validation_calls=validation_calls,
    )

    result = build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    assert (tmp_path / "exported" / "marker.txt").read_text(encoding="utf-8") == "high-goal"
    assert result.best_goal_score == pytest.approx(0.80)
    assert result.best_val_score == pytest.approx(0.10)
    assert validation_calls == [high_goal_low_val]
    payload = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert payload["leader_runtime_hash"] == "high-goal"
    assert payload["best_goal_score"] == pytest.approx(0.80)
    assert payload["selection_policy"] == "goal_score_mean_then_validation"
    assert payload["runtime_abi"] == RUNTIME_ABI_VERSION
    assert payload["export_bundle_file"] == RUNTIME_EXPORT_BUNDLE_FILE
    assert payload["provenance_bundle_file"] == RUNTIME_PROVENANCE_BUNDLE_FILE
    assert len(payload["goal_task_ids"]) == len(goal_score_keys)
    assert payload["runtime_provider"] == "minimax"
    bundle = json.loads((tmp_path / "exported" / RUNTIME_EXPORT_BUNDLE_FILE).read_text(encoding="utf-8"))
    assert bundle["runtime_abi"] == RUNTIME_ABI_VERSION
    assert bundle["runtime_hash"]
    provenance = json.loads((tmp_path / "exported" / RUNTIME_PROVENANCE_BUNDLE_FILE).read_text(encoding="utf-8"))
    assert provenance["schema_version"] == "agintor.runtime.provenance.v1"
    assert provenance["runtime_abi"] == RUNTIME_ABI_VERSION
    assert provenance["attestation_hash"]
    assert "marker.txt" in provenance["artifact_file_digests"]
    assert "runtime_sdk/agintor_runtime/shell.py" in provenance["runtime_identity_inputs"]["immutable_files"]
    assert "agintor/shell.py" not in provenance["runtime_identity_inputs"]["immutable_files"]


def test_build_runtime_uses_validation_only_to_break_goal_ties(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for sandboxed tooling and deterministic execution."
    goal_score_keys = _goal_score_keys(goal_prompt)
    tied_low_val = _write_runtime_dir(tmp_path / "tied_low_val", "tied-low-val")
    tied_high_val = _write_runtime_dir(tmp_path / "tied_high_val", "tied-high-val")
    low_goal_high_val = _write_runtime_dir(tmp_path / "low_goal_high_val", "low-goal-high-val")
    candidates = [
        _candidate_record(
            tied_low_val,
            "tied-low",
            {
                **{key: 0.75 for key in goal_score_keys},
                "sbar:global": 0.45,
            },
        ),
        _candidate_record(
            tied_high_val,
            "tied-high",
            {
                **{key: 0.75 for key in goal_score_keys},
                "sbar:global": 0.42,
            },
        ),
        _candidate_record(
            low_goal_high_val,
            "low-goal",
            {
                **{key: 0.60 for key in goal_score_keys},
                "sbar:global": 0.80,
            },
        ),
    ]
    validation_calls: list[Path] = []
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={"sbar:global": candidates},
        validation_scores={
            str(tied_low_val): 0.30,
            str(tied_high_val): 0.85,
            str(low_goal_high_val): 0.99,
        },
        validation_calls=validation_calls,
    )

    build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    assert (tmp_path / "exported" / "marker.txt").read_text(encoding="utf-8") == "tied-high-val"
    assert set(validation_calls) == {tied_low_val, tied_high_val}
    assert low_goal_high_val not in validation_calls


def test_build_runtime_considers_goal_objective_islands_when_global_island_misses_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    global_only = _write_runtime_dir(tmp_path / "global_only", "global-only")
    goal_only = _write_runtime_dir(tmp_path / "goal_only", "goal-only")
    global_candidate = _candidate_record(
        global_only,
        "global-only",
        {
            **{key: 0.20 for key in goal_score_keys},
            "sbar:global": 0.95,
        },
        objective="sbar:global",
    )
    goal_candidate = _candidate_record(
        goal_only,
        "goal-only",
        {
            **{key: 0.90 for key in goal_score_keys},
            "sbar:global": 0.10,
        },
        objective=goal_score_keys[0],
    )
    validation_calls: list[Path] = []
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            "sbar:global": [global_candidate],
            goal_score_keys[0]: [goal_candidate],
        },
        validation_scores={
            str(global_only): 0.99,
            str(goal_only): 0.10,
        },
        validation_calls=validation_calls,
    )

    result = build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    assert (tmp_path / "exported" / "marker.txt").read_text(encoding="utf-8") == "goal-only"
    assert result.best_goal_score == pytest.approx(0.90)
    assert validation_calls == [goal_only]


def test_build_goal_conditioned_suite_derives_goal_specific_metadata() -> None:
    goal_prompt = "Build a runtime specialized for sandboxed tooling and deterministic execution."
    suite = build_goal_conditioned_suite(goal_prompt, default_runtime_profile())
    goal_tasks = [task for task in suite.train if task.metadata.get("goal_conditioned") is True]

    assert goal_tasks
    assert all(task.metadata["goal_prompt"] == goal_prompt for task in goal_tasks)
    assert all(task.metadata["goal_keywords"] for task in goal_tasks)
    assert any("tool" in task.metadata["target_families"] for task in goal_tasks)
    assert any(str(task.metadata["source_task_id"]).startswith("tool.") for task in goal_tasks)
    assert all("Goal emphasis:" in task.prompt for task in goal_tasks)


def test_build_goal_spec_does_not_treat_build_as_tool_signal() -> None:
    goal_spec = build_goal_spec("Build a runtime specialized for checkpointed memory retrieval.", runtime_provider_name="minimax")

    assert "tool" not in goal_spec.target_families
    assert "tool_reuse_synthesis" not in goal_spec.required_capabilities
    assert goal_spec.target_families[0] == "mem"


def test_build_runtime_forwards_mutator_type_to_evolution_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    runtime_dir = _write_runtime_dir(tmp_path / "candidate", "candidate")
    init_calls: list[dict[str, object]] = []
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            "sbar:global": [
                _candidate_record(
                    runtime_dir,
                    "candidate",
                    {
                        **{key: 0.75 for key in goal_score_keys},
                        "sbar:global": 0.60,
                    },
                )
            ]
        },
        validation_scores={str(runtime_dir): 0.60},
        validation_calls=[],
        init_calls=init_calls,
    )

    build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        mutator_type="provider",
        runtime_backend="local",
    )

    assert init_calls
    assert init_calls[0]["mutator_type"] == "provider"


def test_build_runtime_canonicalizes_goal_text_for_goal_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw_goal_prompt = "Build a runtime specialized for\n checkpointed   memory retrieval."
    canonical_goal_score_keys = _goal_score_keys("Build a runtime specialized for checkpointed memory retrieval.")
    runtime_dir = _write_runtime_dir(tmp_path / "candidate", "candidate")
    validation_calls: list[Path] = []
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            canonical_goal_score_keys[0]: [
                _candidate_record(
                    runtime_dir,
                    "candidate",
                    {
                        **{key: 0.85 for key in canonical_goal_score_keys},
                        "sbar:global": 0.40,
                    },
                    objective=canonical_goal_score_keys[0],
                )
            ]
        },
        validation_scores={str(runtime_dir): 0.50},
        validation_calls=validation_calls,
    )

    result = build_runtime_from_goal(
        raw_goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    assert result.best_goal_score == pytest.approx(0.85)
    payload = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert payload["goal_task_ids"]


def test_build_runtime_considers_archived_runtime_scores_beyond_current_goal_islands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    assert len(goal_score_keys) >= 2
    goal_a, goal_b = goal_score_keys[:2]
    even_candidate = _write_runtime_dir(tmp_path / "even_candidate", "even")
    spiky_candidate = _write_runtime_dir(tmp_path / "spiky_candidate", "spiky")
    candidates = {
        "sbar:global": [
            _candidate_record(
                spiky_candidate,
                "spiky",
                {
                    goal_a: 0.95,
                    goal_b: 0.20,
                    "sbar:global": 0.80,
                },
            )
        ]
    }
    validation_calls: list[Path] = []

    def archive_setup(archive) -> None:
        archive.runtime_dirs["even"] = str(even_candidate)
        archive.runtime_evaluations["even"] = SuiteEvaluation(
            runtime_hash="even",
            objective_scores={
                goal_a: 0.70,
                goal_b: 0.70,
                "sbar:global": 0.30,
            },
            task_scores={},
            family_scores={},
            run_results=[],
            invalid=False,
        )
        archive.runtime_descriptors["even"] = SimpleNamespace(
            code_hash="code-even",
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="seed",
            complexity_bucket=0,
            mutable_loc=1,
        )

    _install_fake_engine(
        monkeypatch,
        candidates_by_objective=candidates,
        validation_scores={
            str(even_candidate): 0.60,
            str(spiky_candidate): 0.40,
        },
        validation_calls=validation_calls,
        archive_setup=archive_setup,
    )

    result = build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    assert (tmp_path / "exported" / "marker.txt").read_text(encoding="utf-8") == "even"
    assert result.best_goal_score == pytest.approx(0.70)



def test_runtime_loader_rejects_manifest_abi_mismatch(runtime_dir: Path, tmp_path: Path) -> None:
    candidate = tmp_path / "runtime_bad_abi"
    shutil.copytree(runtime_dir, candidate)
    manifest_path = candidate / "runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("metadata", {})["runtime_abi"] = "agintor-runtime-abi-v999"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    with pytest.raises(RuntimeLoadError):
        load_runtime(candidate)


def test_build_runtime_raises_when_exported_runtime_is_unloadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    broken_candidate = tmp_path / "broken_candidate"
    broken_candidate.mkdir(parents=True, exist_ok=True)
    (broken_candidate / "marker.txt").write_text("broken", encoding="utf-8")

    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            "sbar:global": [
                _candidate_record(
                    broken_candidate,
                    "broken",
                    {
                        **{key: 0.75 for key in goal_score_keys},
                        "sbar:global": 0.60,
                    },
                )
            ]
        },
        validation_scores={str(broken_candidate): 0.60},
        validation_calls=[],
    )

    with pytest.raises(RuntimeLoadError):
        build_runtime_from_goal(
            goal_prompt,
            destination=tmp_path / "exported",
            workspace=tmp_path / "workspace",
            provider=LocalDeterministicProvider(),
            steps=1,
            runtime_backend="local",
        )


def test_build_runtime_export_omits_generated_bytecode_artifacts(runtime_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    candidate = tmp_path / "candidate_runtime"
    shutil.copytree(runtime_dir, candidate)
    pycache_dir = candidate / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "control_policy.cpython-312.pyc").write_bytes(b"compiled")

    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            "sbar:global": [
                _candidate_record(
                    candidate,
                    "candidate",
                    {
                        **{key: 0.75 for key in goal_score_keys},
                        "sbar:global": 0.60,
                    },
                )
            ]
        },
        validation_scores={str(candidate): 0.60},
        validation_calls=[],
    )

    build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    exported = tmp_path / "exported"
    assert not any("__pycache__" in str(path.relative_to(exported)) for path in exported.rglob("*"))
    provenance = json.loads((exported / RUNTIME_PROVENANCE_BUNDLE_FILE).read_text(encoding="utf-8"))
    assert not any("__pycache__" in path or path.endswith((".pyc", ".pyo")) for path in provenance["artifact_file_digests"])


def test_build_runtime_writes_frozen_workspace_artifact_chain_and_runtime_only_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    candidate_runtime = _write_runtime_dir(tmp_path / "candidate_runtime", "candidate")
    validation_calls: list[Path] = []
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            "sbar:global": [
                _candidate_record(
                    candidate_runtime,
                    "candidate",
                    {
                        **{key: 0.75 for key in goal_score_keys},
                        "sbar:global": 0.60,
                    },
                )
            ]
        },
        validation_scores={str(candidate_runtime): 0.60},
        validation_calls=validation_calls,
        patch_init_runtime=False,
    )

    result = build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="local",
    )

    workspace_root = Path(result.workspace)
    assert (workspace_root / "goal" / "goal_spec.json").exists()
    assert (workspace_root / "goal" / "success_criteria.json").exists()
    assert (workspace_root / "planning" / "benchmark_plan.json").exists()
    assert (workspace_root / "planning" / "benchmark_suite.json").exists()
    assert (workspace_root / "planning" / "verifier_bundle.json").exists()
    assert (workspace_root / "planning" / "runtime_plan.json").exists()
    assert (workspace_root / "planning" / "factory_profile.json").exists()
    assert (workspace_root / "planning" / "deployment_contract.json").exists()
    assert (workspace_root / "planning" / "assumption_register.json").exists()
    assert (workspace_root / "planning" / "planning_diagnostics.json").exists()
    assert (workspace_root / "planning" / "replan_contract.json").exists()
    assert (workspace_root / "export" / "build_summary.json").exists()
    assert (workspace_root / "export" / "export_summary.json").exists()
    assert (workspace_root / "export" / "leaderboard.json").exists()
    assert (workspace_root / "evolution" / "archive_index.json").exists()
    assert (workspace_root / "evolution" / "validation_history.json").exists()
    assert (workspace_root / "evolution" / "stage_failures.json").exists()

    exported_profile = json.loads((tmp_path / "exported" / "runtime_profile.json").read_text(encoding="utf-8"))
    assert "evaluation" not in exported_profile
    assert "evolution" not in exported_profile
    assert exported_profile["execution"]["max_steps"] == 64
    assert (tmp_path / "exported" / "runtime_sdk" / "kernel_manifest.json").exists()

    build_summary = json.loads((workspace_root / "export" / "build_summary.json").read_text(encoding="utf-8"))
    assert build_summary["goal_spec_path"] == str(workspace_root / "goal" / "goal_spec.json")
    assert build_summary["success_criteria_path"] == str(workspace_root / "goal" / "success_criteria.json")
    assert build_summary["benchmark_plan_path"] == str(workspace_root / "planning" / "benchmark_plan.json")
    assert build_summary["verifier_bundle_path"] == str(workspace_root / "planning" / "verifier_bundle.json")
    assert build_summary["runtime_plan_path"] == str(workspace_root / "planning" / "runtime_plan.json")
    assert build_summary["deployment_contract_path"] == str(workspace_root / "planning" / "deployment_contract.json")
    assert build_summary["planning_diagnostics_path"] == str(workspace_root / "planning" / "planning_diagnostics.json")
    manifest = json.loads((tmp_path / "exported" / "runtime_manifest.json").read_text(encoding="utf-8"))
    assert manifest["immutable_manifest"] == [
        "runtime_sdk/kernel_manifest.json",
        "deployment_contract.json",
        "runtime_profile.json",
    ]


def test_build_runtime_freezes_explicit_runtime_backend_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_prompt = "Build a runtime specialized for checkpointed memory retrieval."
    goal_score_keys = _goal_score_keys(goal_prompt)
    candidate_runtime = _write_runtime_dir(tmp_path / "candidate_runtime", "candidate")
    _install_fake_engine(
        monkeypatch,
        candidates_by_objective={
            "sbar:global": [
                _candidate_record(
                    candidate_runtime,
                    "candidate",
                    {
                        **{key: 0.75 for key in goal_score_keys},
                        "sbar:global": 0.60,
                    },
                )
            ]
        },
        validation_scores={str(candidate_runtime): 0.60},
        validation_calls=[],
        patch_init_runtime=False,
    )

    result = build_runtime_from_goal(
        goal_prompt,
        destination=tmp_path / "exported",
        workspace=tmp_path / "workspace",
        provider=LocalDeterministicProvider(),
        steps=1,
        runtime_backend="docker",
    )

    workspace_root = Path(result.workspace)
    goal_spec = json.loads((workspace_root / "goal" / "goal_spec.json").read_text(encoding="utf-8"))
    runtime_plan = json.loads((workspace_root / "planning" / "runtime_plan.json").read_text(encoding="utf-8"))
    assert goal_spec["constraints"]["runtime_backend"] == "docker"
    assert goal_spec["deployment_preferences"]["runtime_backend"] == "docker"
    assert runtime_plan["provider_plan"]["factory_runtime_backend"] == "docker"
    assert runtime_plan["deployment_contract"]["supported_backends"] == ["local", "docker"]


def test_runtime_loader_rejects_unsupported_backend_from_deployment_contract(runtime_dir: Path, tmp_path: Path) -> None:
    candidate = tmp_path / "runtime_bad_backend"
    shutil.copytree(runtime_dir, candidate)
    contract_path = candidate / "deployment_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["supported_backends"] = ["docker"]
    contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

    with pytest.raises(RuntimeLoadError):
        load_runtime(candidate, runtime_backend="local")
