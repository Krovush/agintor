from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_runtime_destination_replace_allows_empty_project_and_preserves_chat(tmp_path: Path) -> None:
    from agintor.factory.service import _replace_runtime_destination

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
    from agintor.factory.service import _replace_runtime_destination

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
    from agintor.factory.service import _replace_runtime_destination

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
    from agintor.factory.service import _replace_runtime_destination

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
    from agintor.factory.service import _write_seed_runtime
    from agintor.runtime.loader import DEPLOYMENT_CONTRACT_FILE, load_runtime
    from agintor.runtime.profile import RUNTIME_PROFILE_FILE, load_runtime_profile
    from agintor.runtime.project import init_runtime

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
