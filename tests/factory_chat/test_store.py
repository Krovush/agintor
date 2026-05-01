from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.contracts import FactoryMessage
from agintor.storage.factory_chat_store import FactoryChatError, FactoryChatStore


def _project(tmp_path: Path, name: str = "factory.project") -> Path:
    project_dir = tmp_path / name
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


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
    store.create_chat(
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
