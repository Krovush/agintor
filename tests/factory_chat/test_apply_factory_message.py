from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.storage.factory_chat_store import FactoryChatError, FactoryChatStore


def _runtime_plan_artifact(tmp_path: Path, name: str = "runtime_plan") -> Path:
    runtime_plan_path = tmp_path / f"{name}.json"
    runtime_plan_path.write_text("{}", encoding="utf-8")
    (runtime_plan_path.parent / "deployment_contract.json").write_text("{}", encoding="utf-8")
    return runtime_plan_path


def _write_fake_runtime_identity(
    runtime_dir: Path,
    runtime_hash: str,
    *,
    runtime_provider: str = "local",
    execution_max_steps: int | None = None,
) -> None:
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import RUNTIME_PROFILE_FILE

    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / RUNTIME_EXPORT_BUNDLE_FILE).write_text(
        json.dumps({"runtime_hash": runtime_hash}),
        encoding="utf-8",
    )
    profile_payload: dict[str, object] = {"runtime_provider": {"name": runtime_provider}}
    if execution_max_steps is not None:
        profile_payload["execution"] = {"max_steps": execution_max_steps}
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps(profile_payload, sort_keys=True),
        encoding="utf-8",
    )


def _stub_live_runtime_hash(monkeypatch, runtime_builder) -> None:
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    import agintor.factory.followups as followups

    def fake_live_hash(runtime_dir: Path, *, runtime_profile, runtime_backend: str) -> str:
        payload = json.loads((Path(runtime_dir) / RUNTIME_EXPORT_BUNDLE_FILE).read_text(encoding="utf-8"))
        return str(payload["runtime_hash"])

    monkeypatch.setattr(followups, "_load_current_runtime_hash", fake_live_hash)


def test_apply_factory_message_routes_initial_then_followup(tmp_path: Path, monkeypatch) -> None:
    """`apply_factory_message` creates a chat on the first call and routes subsequent
    calls through `build_runtime_from_followup` against the recorded prior goal."""

    import agintor.factory.service as runtime_builder
    from agintor.contracts import GoalSpec
    from agintor.factory.service import (
        BuiltRuntimeResult,
        apply_factory_message,
        build_runtime_from_followup,
        build_runtime_from_goal,
    )
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import load_runtime_profile

    initial_goal_path = tmp_path / "initial_goal.json"
    follow_goal_path = tmp_path / "follow_goal.json"
    follow_goal_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(tmp_path)
    relative_project_arg = Path("project.under_test")
    project_dir = (tmp_path / "project.under_test").resolve()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    initial_goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build a memory retrieval runtime",
                "normalized_goal": "build a memory retrieval runtime",
                "goal_keywords": ["memory", "retrieval"],
                "goal_phrases": ["memory retrieval"],
                "required_capabilities": [],
                "constraints": {},
                "success_criteria": [],
                "target_families": ["mem"],
                "deployment_preferences": {},
                "assumptions": [],
                "amendment_index": 0,
                "amendment_history": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    invocations: list[dict] = []

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_initial")
        trace_context = kwargs.get("trace_context")
        invocations.append(
            {
                "call": "initial",
                "prompt": prompt,
                "runtime_backend": kwargs.get("runtime_backend"),
                "trace_context": trace_context.model_dump() if trace_context is not None else None,
            }
        )
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(initial_goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type=kwargs.get("mutator_type", "heuristic"),
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    def fake_followup_build(prior_goal: GoalSpec, instruction: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.beta",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_follow")
        amended = (prior_goal).model_copy(
            update={
                "amendment_index": prior_goal.amendment_index + 1,
                "amendment_history": list(prior_goal.amendment_history) + [instruction],
                "raw_prompt": instruction,
            }
        )
        follow_goal_path.write_text(
            json.dumps((amended).model_dump(), sort_keys=True),
            encoding="utf-8",
        )
        invocations.append(
            {
                "call": "followup",
                "instruction": instruction,
                "prior_goal_id": prior_goal.goal_id,
                "seed_runtime_source": kwargs.get("seed_runtime_source"),
                "runtime_provider_name": kwargs.get("runtime_provider_name"),
                "runtime_backend": kwargs.get("runtime_backend"),
                "trace_context": kwargs.get("trace_context").model_dump()
                if kwargs.get("trace_context") is not None
                else None,
            }
        )
        return BuiltRuntimeResult(
            build_id="build.followup",
            goal_id=prior_goal.goal_id,
            goal_prompt=instruction,
            goal_spec_path=str(follow_goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type=kwargs.get("mutator_type", "heuristic"),
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    monkeypatch.setattr(runtime_builder, "build_runtime_from_followup", fake_followup_build)

    provider = LocalDeterministicProvider()
    initial = apply_factory_message(
        relative_project_arg,
        "Build a memory retrieval runtime",
        workspace=workspace_dir,
        provider=provider,
        steps=1,
        mutator_type="heuristic",
        runtime_backend="docker",
    )

    assert invocations[0]["call"] == "initial"
    assert invocations[0]["prompt"] == "Build a memory retrieval runtime"
    assert invocations[0]["runtime_backend"] == "docker"
    assert invocations[0]["trace_context"]["factory_message_index"] == 0
    assert initial.chat.message_count == 1
    assert initial.message.message_index == 0
    assert invocations[0]["trace_context"]["factory_chat_id"] == initial.chat.chat_id
    assert invocations[0]["trace_context"]["factory_message_id"] == initial.message.message_id
    assert initial.message.parent_message_id is None
    assert initial.message.leader_runtime_hash == "runtime.hash.alpha"
    assert (project_dir / ".factory_chat" / "manifest.json").exists()

    follow = apply_factory_message(
        project_dir,
        "Also surface citations alongside retrieved evidence.",
        workspace=workspace_dir,
        provider=provider,
        steps=1,
        mutator_type="heuristic",
    )

    assert len(invocations) == 2
    assert invocations[1]["call"] == "followup"
    assert invocations[1]["prior_goal_id"] == "goal.alpha"
    assert invocations[1]["instruction"] == "Also surface citations alongside retrieved evidence."
    assert invocations[1]["seed_runtime_source"] == project_dir
    assert invocations[1]["runtime_provider_name"] == default_runtime_provider
    assert invocations[1]["runtime_backend"] == "docker"
    assert invocations[1]["trace_context"]["factory_chat_id"] == initial.chat.chat_id
    assert invocations[1]["trace_context"]["factory_message_index"] == 1
    assert follow.chat.chat_id == initial.chat.chat_id
    assert follow.chat.message_count == 2
    assert follow.message.message_index == 1
    assert follow.message.parent_message_id == initial.message.message_id
    assert follow.message.leader_runtime_hash == "runtime.hash.beta"


def test_apply_factory_message_rejects_followup_identity_drift(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import load_runtime_profile

    project_dir = tmp_path / "project.identity"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_identity")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )

    with pytest.raises(FactoryChatError, match="runtime backend"):
        apply_factory_message(
            project_dir,
            "Use Docker now",
            workspace=workspace_dir,
            provider=LocalDeterministicProvider(),
            runtime_backend="docker",
        )


def test_apply_factory_message_initial_build_preserves_project_side_files(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import load_runtime_profile

    project_dir = tmp_path / "project.with-notes"
    project_dir.mkdir()
    (project_dir / "notes.md").write_text("keep this", encoding="utf-8")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    captured = {}
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        captured["force"] = kwargs.get("force")
        destination = Path(kwargs["destination"])
        from agintor.factory.service import _replace_runtime_destination

        source = tmp_path / "source-runtime"
        _write_fake_runtime_identity(
            source,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        _replace_runtime_destination(source, destination, force=kwargs["force"])
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_preserve")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)

    outcome = apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )

    assert captured["force"] is True
    assert (project_dir / "notes.md").read_text(encoding="utf-8") == "keep this"
    assert (project_dir / RUNTIME_EXPORT_BUNDLE_FILE).exists()
    assert outcome.chat.message_count == 1


def test_apply_factory_message_uses_embedded_profile_on_followup(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import RUNTIME_PROFILE_FILE

    project_dir = tmp_path / "project.embedded-profile"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )

    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider="minimax",
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_embedded_initial")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider="minimax",
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    captured_followup: dict[str, object] = {}

    def fake_followup_build(prior_goal, instruction: str, **kwargs):
        captured_followup.update(kwargs)
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.beta",
            runtime_provider="minimax",
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_embedded_follow")
        return BuiltRuntimeResult(
            build_id="build.followup",
            goal_id=prior_goal.goal_id,
            goal_prompt=instruction,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider="minimax",
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    monkeypatch.setattr(runtime_builder, "build_runtime_from_followup", fake_followup_build)

    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )
    apply_factory_message(
        project_dir,
        "Keep going",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )

    assert captured_followup["runtime_provider_name"] == "minimax"
    assert Path(captured_followup["profile_path"]) == project_dir / RUNTIME_PROFILE_FILE
    assert captured_followup["runtime_profile"].runtime_provider.name == "minimax"


def test_apply_factory_message_rejects_profile_override_on_followup(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import load_runtime_profile

    project_dir = tmp_path / "project.profile-pinned"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_profile_pinned")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )
    override_profile = tmp_path / "override_profile.json"
    override_profile.write_text(json.dumps({"execution": {"max_steps": 7}}), encoding="utf-8")

    with pytest.raises(FactoryChatError, match="pinned in the project"):
        apply_factory_message(
            project_dir,
            "Keep going",
            workspace=workspace_dir,
            provider=LocalDeterministicProvider(),
            profile_path=override_profile,
            runtime_backend="local",
        )


def test_apply_factory_message_rejects_tampered_runtime_hash_on_followup(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import load_runtime_profile

    project_dir = tmp_path / "project.hash-pinned"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_hash_pinned")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    monkeypatch.setattr(
        runtime_builder,
        "build_runtime_from_followup",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("followup should not run")),
    )
    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )
    (project_dir / RUNTIME_EXPORT_BUNDLE_FILE).write_text(
        json.dumps({"runtime_hash": "runtime.hash.tampered"}),
        encoding="utf-8",
    )

    with pytest.raises(FactoryChatError, match="pinned to runtime hash"):
        apply_factory_message(
            project_dir,
            "Keep going",
            workspace=workspace_dir,
            provider=LocalDeterministicProvider(),
            runtime_backend="local",
        )


def test_apply_factory_message_rejects_live_runtime_hash_drift_on_followup(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.profile import load_runtime_profile

    project_dir = tmp_path / "project.live-hash-pinned"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    live_hashes = iter(["runtime.hash.alpha", "runtime.hash.changed"])

    def fake_live_hash(runtime_dir: Path, *, runtime_profile, runtime_backend: str) -> str:
        return next(live_hashes)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_live_hash_pinned")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file="runtime_export_bundle.json",
            export_summary_path="",
            summary_path="",
        )

    import agintor.factory.followups as followups

    monkeypatch.setattr(followups, "_load_current_runtime_hash", fake_live_hash)
    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    monkeypatch.setattr(
        runtime_builder,
        "build_runtime_from_followup",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("followup should not run")),
    )

    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )

    with pytest.raises(FactoryChatError, match="pinned to runtime hash"):
        apply_factory_message(
            project_dir,
            "Keep going",
            workspace=workspace_dir,
            provider=LocalDeterministicProvider(),
            runtime_backend="local",
        )


def test_apply_factory_message_rejects_tampered_runtime_profile_on_followup(tmp_path: Path, monkeypatch) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.profile import RUNTIME_PROFILE_FILE, load_runtime_profile

    project_dir = tmp_path / "project.profile-tampered"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_profile_tampered")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file="runtime_export_bundle.json",
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    monkeypatch.setattr(
        runtime_builder,
        "build_runtime_from_followup",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("followup should not run")),
    )

    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )
    (project_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"runtime_provider": {"name": default_runtime_provider}, "execution": {"max_steps": 7}}),
        encoding="utf-8",
    )

    with pytest.raises(FactoryChatError, match="runtime profile hash"):
        apply_factory_message(
            project_dir,
            "Keep going",
            workspace=workspace_dir,
            provider=LocalDeterministicProvider(),
            runtime_backend="local",
        )


def test_apply_factory_message_followup_missing_artifact_does_not_commit_runtime_or_partial_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agintor.factory.service as runtime_builder
    from agintor.factory.service import BuiltRuntimeResult, apply_factory_message
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime.loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime.profile import load_runtime_profile

    project_dir = tmp_path / "project.followup-artifact-failclosed"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    goal_path = tmp_path / "goal.json"
    goal_path.write_text(
        json.dumps(
            {
                "goal_id": "goal.alpha",
                "raw_prompt": "Build runtime",
                "normalized_goal": "build runtime",
                "target_families": ["top"],
            },
        ),
        encoding="utf-8",
    )
    default_runtime_provider = load_runtime_profile().runtime_provider.name
    _stub_live_runtime_hash(monkeypatch, runtime_builder)

    def fake_initial_build(prompt: str, **kwargs):
        destination = Path(kwargs["destination"])
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.alpha",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_initial_artifact_failclosed")
        return BuiltRuntimeResult(
            build_id="build.initial",
            goal_id="goal.alpha",
            goal_prompt=prompt,
            goal_spec_path=str(goal_path),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file="runtime_export_bundle.json",
            export_summary_path="",
            summary_path="",
        )

    def fake_followup_build(prior_goal, instruction: str, **kwargs):
        destination = Path(kwargs["destination"])
        assert destination != project_dir
        _write_fake_runtime_identity(
            destination,
            "runtime.hash.beta",
            runtime_provider=default_runtime_provider,
        )
        runtime_plan_path = _runtime_plan_artifact(tmp_path, "runtime_plan_followup_missing_artifact")
        return BuiltRuntimeResult(
            build_id="build.followup",
            goal_id="goal.alpha",
            goal_prompt=instruction,
            goal_spec_path=str(tmp_path / "missing_goal_spec.json"),
            success_criteria_path="",
            benchmark_plan_path="",
            verifier_bundle_path="",
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(kwargs["workspace"]),
            agintor_provider="local",
            runtime_provider=default_runtime_provider,
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file="runtime_export_bundle.json",
            export_summary_path="",
            summary_path="",
        )

    monkeypatch.setattr(runtime_builder, "build_runtime_from_goal", fake_initial_build)
    monkeypatch.setattr(runtime_builder, "build_runtime_from_followup", fake_followup_build)

    apply_factory_message(
        project_dir,
        "Build runtime",
        workspace=workspace_dir,
        provider=LocalDeterministicProvider(),
        runtime_backend="local",
    )

    with pytest.raises(FactoryChatError, match="planning artifact"):
        apply_factory_message(
            project_dir,
            "Keep going",
            workspace=workspace_dir,
            provider=LocalDeterministicProvider(),
            runtime_backend="local",
        )

    export_bundle = json.loads((project_dir / RUNTIME_EXPORT_BUNDLE_FILE).read_text(encoding="utf-8"))
    assert export_bundle["runtime_hash"] == "runtime.hash.alpha"
    store = FactoryChatStore(project_dir)
    assert len(store.messages()) == 1
    assert list((store.root / "messages").glob("0001_*")) == []
