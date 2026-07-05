from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.contracts import GoalSpec, baseline_langgraph_runtime_spec
from agintor.factory.followups import apply_factory_message
from agintor.factory.service import BuiltRuntimeResult
from agintor.providers import LocalDeterministicProvider
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.loader import load_runtime
from agintor.runtime.profile import RUNTIME_PROFILE_FILE, load_runtime_profile, runtime_profile_payload
from agintor.storage.factory_chat_store import FactoryChatError, FactoryChatStore


def _write_fake_artifacts(root: Path, prompt: str) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    goal = GoalSpec(
        goal_id="goal.pinning",
        raw_prompt=prompt,
        normalized_goal=prompt,
        constraints={"runtime_kind": "langgraph_spec"},
    )
    payloads = {
        "goal_spec_path": goal.model_dump(mode="json"),
        "success_criteria_path": {"criteria": []},
        "benchmark_plan_path": {"plan_id": "benchmark-plan.pinning"},
        "verifier_bundle_path": {"bundle_id": "verifier-bundle.pinning"},
        "runtime_plan_path": {"runtime_kind": "langgraph_spec"},
        "export_summary_path": {"runtime_kind": "langgraph_spec"},
        "summary_path": {"runtime_kind": "langgraph_spec"},
        "signal_sufficiency_path": {"status": "insufficient"},
    }
    paths: dict[str, str] = {}
    for field_name, payload in payloads.items():
        path = root / f"{field_name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        paths[field_name] = str(path)
    (root / "deployment_contract.json").write_text("{}", encoding="utf-8")
    return paths


def _write_fake_runtime(destination: Path) -> str:
    profile = load_runtime_profile()
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id=f"runtime.{destination.name}"),
        destination,
        force=True,
    )
    (destination / RUNTIME_PROFILE_FILE).write_text(
        json.dumps(runtime_profile_payload(profile), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return load_runtime(destination, runtime_profile=profile, runtime_backend="local").runtime_hash


def _result(destination: Path, workspace: Path, prompt: str, runtime_hash: str, runtime_kind: str) -> BuiltRuntimeResult:
    artifacts = _write_fake_artifacts(workspace / f"artifacts-{destination.name}", prompt)
    return BuiltRuntimeResult(
        build_id=f"build.{destination.name}",
        goal_id="goal.pinning",
        goal_prompt=prompt,
        output_runtime_dir=str(destination),
        workspace=str(workspace),
        agintor_provider="local",
        runtime_provider="openai",
        mutator_type="heuristic",
        best_train_score=0.0,
        best_goal_score=0.0,
        best_val_score=0.0,
        archive_cells=1,
        accepted_mutations=0,
        export_bundle_file="runtime_export_bundle.json",
        runtime_kind=runtime_kind,
        runtime_spec_digest="spec.digest",
        oracle_package_hash="oracle.hash",
        oracle_package_ref="",
        oracle_public_ref="",
        **artifacts,
    )


def test_factory_runtime_kind_pinning_reuses_kind_for_followups_and_rejects_switches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agintor.factory.service as service

    project_dir = tmp_path / "project"
    workspace = tmp_path / "workspace"
    calls: list[tuple[str, str]] = []

    def fake_build_from_goal(goal_prompt: str, *, destination, workspace, runtime_kind: str, **_kwargs):
        calls.append(("goal", runtime_kind))
        destination = Path(destination)
        runtime_hash = _write_fake_runtime(destination)
        return _result(destination, Path(workspace), goal_prompt, runtime_hash, runtime_kind)

    def fake_build_from_followup(prior_goal, instruction: str, *, destination, workspace, runtime_kind: str, **_kwargs):
        calls.append(("followup", runtime_kind))
        destination = Path(destination)
        runtime_hash = _write_fake_runtime(destination)
        return _result(destination, Path(workspace), instruction, runtime_hash, runtime_kind)

    monkeypatch.setattr(service, "build_runtime_from_goal", fake_build_from_goal)
    monkeypatch.setattr(service, "build_runtime_from_followup", fake_build_from_followup)

    first = apply_factory_message(
        project_dir,
        "build a spec runtime",
        workspace=workspace,
        provider=LocalDeterministicProvider(),
        steps=0,
        runtime_kind="langgraph_spec",
        artifact_mode="none",
    )
    second = apply_factory_message(
        project_dir,
        "tighten the runtime",
        workspace=workspace,
        provider=LocalDeterministicProvider(),
        steps=0,
        artifact_mode="none",
    )

    store = FactoryChatStore(project_dir)
    messages = store.messages()

    assert calls == [("goal", "langgraph_spec"), ("followup", "langgraph_spec")]
    assert first.chat.runtime_kind == "langgraph_spec"
    assert second.chat.runtime_kind == "langgraph_spec"
    assert [message.runtime_kind for message in messages] == ["langgraph_spec", "langgraph_spec"]

    with pytest.raises(FactoryChatError, match="start a new factory chat"):
        apply_factory_message(
            project_dir,
            "switch to trading agents",
            workspace=workspace,
            provider=LocalDeterministicProvider(),
            steps=0,
            runtime_kind="tradingagents_langgraph",
            artifact_mode="none",
        )

    assert FactoryChatStore(project_dir).load_chat().message_count == 2
    assert len(FactoryChatStore(project_dir).messages()) == 2
