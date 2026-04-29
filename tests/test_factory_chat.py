from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agintor.factory_chat_store import FactoryChatError, FactoryChatStore
from agintor.goal_rubric import amend_goal_spec, build_goal_spec
from agintor.schemas import FactoryMessage


def _project(tmp_path: Path, name: str = "factory.project") -> Path:
    project_dir = tmp_path / name
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


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
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import RUNTIME_PROFILE_FILE

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
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE

    def fake_live_hash(runtime_dir: Path, *, runtime_profile, runtime_backend: str) -> str:
        payload = json.loads((Path(runtime_dir) / RUNTIME_EXPORT_BUNDLE_FILE).read_text(encoding="utf-8"))
        return str(payload["runtime_hash"])

    monkeypatch.setattr(runtime_builder, "_load_current_runtime_hash", fake_live_hash)


def test_factory_chat_store_create_and_load(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    store = FactoryChatStore(project_dir)
    assert not store.has_chat()

    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    assert chat.chat_id.startswith("chat.")
    assert chat.goal_id == "goal.alpha"
    assert chat.runtime_provider == "local"
    assert chat.runtime_backend == "local"
    assert chat.message_count == 0
    assert chat.last_message_id is None

    loaded = store.load_chat()
    assert loaded.chat_id == chat.chat_id
    assert loaded.goal_id == chat.goal_id


def test_factory_chat_store_rejects_double_create(tmp_path: Path) -> None:
    store = FactoryChatStore(_project(tmp_path))
    store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    with pytest.raises(FactoryChatError):
        store.create_chat(
            goal_id="goal.alpha",
            runtime_provider="local",
            agintor_provider="local",
            runtime_backend="local",
        )


def test_factory_chat_store_record_message_copies_artifacts(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    store = FactoryChatStore(project_dir)
    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    goal_path = artifact_dir / "goal_spec.json"
    success_path = artifact_dir / "success_criteria.json"
    goal_path.write_text(json.dumps({"goal_id": "goal.alpha"}), encoding="utf-8")
    success_path.write_text(json.dumps({"goal_id": "goal.alpha"}), encoding="utf-8")

    message = FactoryMessage(
        message_id=store.allocate_message_id(message_index=0, prompt="initial"),
        message_index=0,
        parent_message_id=None,
        chat_id=chat.chat_id,
        prompt="initial",
        build_id="build.test",
        leader_runtime_hash="runtime.hash",
    )
    recorded = store.record_message(
        message,
        prompt_text="initial",
        planning_artifacts={
            "goal_spec_path": goal_path,
            "success_criteria_path": success_path,
        },
    )
    assert Path(recorded.goal_spec_path).exists()
    assert Path(recorded.success_criteria_path).exists()
    assert Path(recorded.goal_spec_path).parent != goal_path.parent

    refreshed = store.load_chat()
    assert refreshed.message_count == 1
    assert refreshed.last_message_id == message.message_id

    latest = store.latest_message()
    assert latest is not None
    assert latest.message_id == message.message_id
    assert latest.prompt == "initial"


def test_factory_chat_store_rejects_missing_declared_artifact(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    store = FactoryChatStore(project_dir)
    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    message = FactoryMessage(
        message_id=store.allocate_message_id(message_index=0, prompt="initial"),
        message_index=0,
        parent_message_id=None,
        chat_id=chat.chat_id,
        prompt="initial",
        build_id="build.test",
        leader_runtime_hash="runtime.hash",
    )

    with pytest.raises(FactoryChatError, match="planning artifact"):
        store.record_message(
            message,
            prompt_text="initial",
            planning_artifacts={"goal_spec_path": tmp_path / "missing_goal_spec.json"},
        )
    assert list((store.root / "messages").iterdir()) == []


def test_factory_chat_store_messages_are_ordered_by_index(tmp_path: Path) -> None:
    store = FactoryChatStore(_project(tmp_path))
    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    for idx in range(3):
        message = FactoryMessage(
            message_id=store.allocate_message_id(message_index=idx, prompt=f"msg-{idx}"),
            message_index=idx,
            parent_message_id=None,
            chat_id=chat.chat_id,
            prompt=f"msg-{idx}",
            build_id=f"build.{idx}",
            leader_runtime_hash="runtime.hash",
        )
        store.record_message(message, prompt_text=f"msg-{idx}", planning_artifacts={})
    indices = [message.message_index for message in store.messages()]
    assert indices == [0, 1, 2]
    assert store.next_message_index() == 3


def test_factory_chat_store_rejects_out_of_order_messages(tmp_path: Path) -> None:
    store = FactoryChatStore(_project(tmp_path))
    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    message = FactoryMessage(
        message_id=store.allocate_message_id(message_index=1, prompt="late"),
        message_index=1,
        parent_message_id=None,
        chat_id=chat.chat_id,
        prompt="late",
        build_id="build.late",
        leader_runtime_hash="runtime.hash",
    )

    with pytest.raises(FactoryChatError, match="expected message_index 0"):
        store.record_message(message, prompt_text="late", planning_artifacts={})


def test_factory_chat_store_rejects_corrupt_message_metadata(tmp_path: Path) -> None:
    store = FactoryChatStore(_project(tmp_path))
    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    message = FactoryMessage(
        message_id=store.allocate_message_id(message_index=0, prompt="initial"),
        message_index=0,
        parent_message_id=None,
        chat_id=chat.chat_id,
        prompt="initial",
        build_id="build.test",
        leader_runtime_hash="runtime.hash",
    )
    recorded = store.record_message(message, prompt_text="initial", planning_artifacts={})
    metadata_path = (
        store.root
        / "messages"
        / f"{recorded.message_index:04d}_{recorded.message_id}"
        / "metadata.json"
    )
    metadata_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(FactoryChatError, match="message metadata"):
        store.latest_message()


def test_factory_chat_store_rejects_missing_message_metadata(tmp_path: Path) -> None:
    store = FactoryChatStore(_project(tmp_path))
    chat = store.create_chat(
        goal_id="goal.alpha",
        runtime_provider="local",
        agintor_provider="local",
        runtime_backend="local",
    )
    message_dir = store.root / "messages" / "0000_msg.missing"
    message_dir.mkdir(parents=True)
    (message_dir / "prompt.txt").write_text("partial", encoding="utf-8")

    with pytest.raises(FactoryChatError, match="metadata is missing"):
        store.latest_message()


def test_factory_chat_store_rejects_load_for_uninitialized_project(tmp_path: Path) -> None:
    store = FactoryChatStore(_project(tmp_path))
    with pytest.raises(FactoryChatError):
        store.load_chat()


def test_amend_goal_spec_preserves_goal_id_and_extends_history() -> None:
    initial = build_goal_spec(
        "Build a memory retrieval runtime",
        runtime_provider_name="local",
        default_runtime_backend="local",
    )
    assert initial.amendment_index == 0
    assert initial.amendment_history == []

    amended = amend_goal_spec(
        initial,
        "Also surface citations alongside retrieved evidence.",
        runtime_provider_name="local",
        default_runtime_backend="local",
    )
    assert amended.goal_id == initial.goal_id
    assert amended.amendment_index == 1
    assert amended.amendment_history == [
        "Also surface citations alongside retrieved evidence.",
    ]
    assert "citations" in amended.normalized_goal
    assert amended.raw_prompt == "Also surface citations alongside retrieved evidence."

    twice = amend_goal_spec(
        amended,
        "Prefer cheaper providers when quality is comparable.",
        runtime_provider_name="local",
        default_runtime_backend="local",
    )
    assert twice.goal_id == initial.goal_id
    assert twice.amendment_index == 2
    assert len(twice.amendment_history) == 2


def test_amend_goal_spec_rejects_empty_instruction() -> None:
    initial = build_goal_spec(
        "Build a memory retrieval runtime",
        runtime_provider_name="local",
    )
    with pytest.raises(ValueError):
        amend_goal_spec(initial, "")


def test_runtime_destination_replace_allows_empty_project_and_preserves_chat(tmp_path: Path) -> None:
    from agintor.runtime_builder import _replace_runtime_destination

    source = tmp_path / "source_runtime"
    source.mkdir()
    (source / "runtime_manifest.json").write_text("{}", encoding="utf-8")

    empty_destination = tmp_path / "empty_project"
    empty_destination.mkdir()
    _replace_runtime_destination(source, empty_destination, force=False)
    assert (empty_destination / "runtime_manifest.json").exists()

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "runtime_manifest.json").write_text('{"new": true}', encoding="utf-8")
    chat_dir = empty_destination / ".factory_chat"
    chat_dir.mkdir()
    (chat_dir / "manifest.json").write_text("{}", encoding="utf-8")

    _replace_runtime_destination(
        replacement,
        empty_destination,
        force=True,
        preserve_names=(".factory_chat",),
    )
    assert json.loads((empty_destination / "runtime_manifest.json").read_text(encoding="utf-8")) == {"new": True}
    assert (empty_destination / ".factory_chat" / "manifest.json").exists()


def test_runtime_destination_replace_preserves_project_side_files(tmp_path: Path) -> None:
    from agintor.runtime_builder import _replace_runtime_destination

    source = tmp_path / "source_runtime"
    source.mkdir()
    (source / "runtime_manifest.json").write_text("new-runtime", encoding="utf-8")
    destination = tmp_path / "project"
    destination.mkdir()
    (destination / "notes.md").write_text("keep me", encoding="utf-8")
    (destination / "runtime_manifest.json").write_text("old-runtime", encoding="utf-8")

    _replace_runtime_destination(source, destination, force=True)

    assert (destination / "runtime_manifest.json").read_text(encoding="utf-8") == "new-runtime"
    assert (destination / "notes.md").read_text(encoding="utf-8") == "keep me"


def test_runtime_destination_replace_rejects_nested_source_and_destination(tmp_path: Path) -> None:
    from agintor.runtime_builder import _replace_runtime_destination

    destination = tmp_path / "project"
    source = destination / ".agintor_evo" / "leader"
    source.mkdir(parents=True)
    (source / "runtime_manifest.json").write_text("new-runtime", encoding="utf-8")
    destination_marker = destination / "runtime_manifest.json"
    destination_marker.write_text("old-runtime", encoding="utf-8")

    with pytest.raises(ValueError, match="nested source"):
        _replace_runtime_destination(source, destination, force=True)

    assert destination_marker.read_text(encoding="utf-8") == "old-runtime"
    assert source.exists()


@pytest.mark.parametrize(
    ("source_builder", "destination_builder", "expected_marker"),
    [
        (
            lambda root: root / "runtime",
            lambda root: root / "runtime",
            lambda root: root / "runtime" / "runtime_manifest.json",
        ),
        (
            lambda root: root / "leader",
            lambda root: root / "leader" / "nested_project",
            lambda root: root / "leader" / "nested_project" / "runtime_manifest.json",
        ),
    ],
)
def test_runtime_destination_replace_rejects_equal_and_destination_inside_source(
    tmp_path: Path,
    source_builder,
    destination_builder,
    expected_marker,
) -> None:
    from agintor.runtime_builder import _replace_runtime_destination

    source = source_builder(tmp_path)
    destination = destination_builder(tmp_path)
    source.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    source_marker = source / "source_only.txt"
    destination_marker = expected_marker(tmp_path)
    source_marker.write_text("source still here", encoding="utf-8")
    destination_marker.write_text("destination still here", encoding="utf-8")

    with pytest.raises(ValueError, match="nested source"):
        _replace_runtime_destination(source, destination, force=True)

    assert source_marker.read_text(encoding="utf-8") == "source still here"
    assert destination_marker.read_text(encoding="utf-8") == "destination still here"


def test_seed_runtime_copy_excludes_project_side_files(tmp_path: Path) -> None:
    from agintor.project import init_runtime
    from agintor.runtime_builder import _write_seed_runtime
    from agintor.runtime_loader import DEPLOYMENT_CONTRACT_FILE, load_runtime
    from agintor.runtime_profile import RUNTIME_PROFILE_FILE, load_runtime_profile

    source = init_runtime(tmp_path / "source-runtime", force=True)
    (source / "notes.md").write_text("project note", encoding="utf-8")
    (source / ".factory_chat").mkdir()
    (source / ".runtime_sessions").mkdir()

    seed = tmp_path / "seed-runtime"
    runtime_plan = SimpleNamespace(
        runtime_profile=json.loads((source / RUNTIME_PROFILE_FILE).read_text(encoding="utf-8")),
        deployment_contract=json.loads((source / DEPLOYMENT_CONTRACT_FILE).read_text(encoding="utf-8")),
    )
    _write_seed_runtime(
        seed,
        runtime_plan,
        seed_source=source,
        runtime_profile=load_runtime_profile(source),
        runtime_backend="local",
    )

    assert (seed / "topology_policy.py").exists()
    assert (seed / "runtime_sdk" / "kernel_manifest.json").exists()
    assert load_runtime(seed, runtime_backend="local").runtime_hash
    assert not (seed / "notes.md").exists()
    assert not (seed / ".factory_chat").exists()
    assert not (seed / ".runtime_sessions").exists()


def test_apply_factory_message_routes_initial_then_followup(tmp_path: Path, monkeypatch) -> None:
    """`apply_factory_message` creates a chat on the first call and routes subsequent
    calls through `build_runtime_from_followup` against the recorded prior goal."""

    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import (
        BuiltRuntimeResult,
        apply_factory_message,
        build_runtime_from_followup,
        build_runtime_from_goal,
    )
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import load_runtime_profile
    from agintor.schemas import GoalSpec

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import load_runtime_profile

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import load_runtime_profile

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
        from agintor.runtime_builder import _replace_runtime_destination

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import RUNTIME_PROFILE_FILE

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import load_runtime_profile

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import load_runtime_profile

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_profile import load_runtime_profile

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

    monkeypatch.setattr(runtime_builder, "_load_current_runtime_hash", fake_live_hash)
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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_profile import RUNTIME_PROFILE_FILE, load_runtime_profile

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
    from agintor import runtime_builder
    from agintor.providers import LocalDeterministicProvider
    from agintor.runtime_builder import BuiltRuntimeResult, apply_factory_message
    from agintor.runtime_loader import RUNTIME_EXPORT_BUNDLE_FILE
    from agintor.runtime_profile import load_runtime_profile

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


def test_build_runtime_cli_accepts_documented_destination_form(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agintor import cli
    from agintor.cli import app

    project_dir = tmp_path / "project.cli"
    workspace_dir = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            captured["released_failed"] = failed

    def fake_apply_factory_message(project_dir_arg, instruction, **kwargs):
        captured["project_dir"] = project_dir_arg
        captured["instruction"] = instruction
        captured["workspace"] = kwargs["workspace"]
        return SimpleNamespace(
            chat=SimpleNamespace(
                chat_id="chat.cli",
                project_dir=str(project_dir),
            ),
            message=SimpleNamespace(
                message_id="msg.cli",
                message_index=0,
                parent_message_id=None,
                leader_runtime_hash="runtime.hash.cli",
                leader_runtime_dir=str(project_dir),
                build_id="build.cli",
            ),
            result=SimpleNamespace(build_id="build.cli"),
        )

    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(cli, "apply_factory_message", fake_apply_factory_message)

    result = CliRunner().invoke(
        app,
        ["build-runtime", "Build runtime", "--destination", str(project_dir), "--steps", "1"],
    )

    assert result.exit_code == 0, result.output
    assert captured["project_dir"] == str(project_dir)
    assert captured["instruction"] == "Build runtime"
    assert captured["workspace"] == workspace_dir
    assert captured["released_failed"] is False


def test_build_runtime_cli_accepts_project_prompt_chat_form(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agintor import cli
    from agintor.cli import app

    project_dir = tmp_path / "project.chat"
    workspace_dir = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            captured["released_failed"] = failed

    def fake_apply_factory_message(project_dir_arg, instruction, **kwargs):
        captured["project_dir"] = project_dir_arg
        captured["instruction"] = instruction
        captured["workspace"] = kwargs["workspace"]
        captured["runtime_backend"] = kwargs["runtime_backend"]
        return SimpleNamespace(
            chat=SimpleNamespace(
                chat_id="chat.cli",
                project_dir=str(project_dir),
            ),
            message=SimpleNamespace(
                message_id="msg.cli",
                message_index=0,
                parent_message_id=None,
                leader_runtime_hash="runtime.hash.cli",
                leader_runtime_dir=str(project_dir),
                build_id="build.cli",
            ),
            result=SimpleNamespace(build_id="build.cli"),
        )

    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(cli, "apply_factory_message", fake_apply_factory_message)

    result = CliRunner().invoke(
        app,
        ["build-runtime", str(project_dir), "--prompt", "Build chat runtime", "--runtime-backend", "docker"],
    )

    assert result.exit_code == 0, result.output
    assert captured["project_dir"] == str(project_dir)
    assert captured["instruction"] == "Build chat runtime"
    assert captured["workspace"] == workspace_dir
    assert captured["runtime_backend"] == "docker"
    assert captured["released_failed"] is False


def test_provider_mutator_requests_carry_factory_trace_context() -> None:
    from agintor.mutator import ProviderPatchMutator
    from agintor.schemas import ModelResponse, OpenAITraceContext

    captured = {}

    class CapturingProvider:
        provider_name = "openai"

        def generate(self, request):
            captured["metadata"] = request.metadata
            return ModelResponse(text="<<<SEARCH\nold\n===\nnew\n>>>REPLACE")

    trace_context = OpenAITraceContext(
        factory_chat_id="chat.alpha",
        factory_message_id="fmsg.1",
        factory_message_index=1,
    )
    mutator = ProviderPatchMutator(CapturingProvider())

    mutator._request_patch(
        instructions="Return a patch.",
        prompt="Patch something.",
        model_class="large",
        seed=0,
        mode="patch",
        trace_context=trace_context,
    )

    metadata = captured["metadata"]
    assert metadata["trace_context"]["factory_chat_id"] == "chat.alpha"
    assert metadata["trace_context"]["factory_message_id"] == "fmsg.1"
