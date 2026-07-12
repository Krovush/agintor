from __future__ import annotations

from pathlib import Path

import pytest

from agintor.contracts.epochs import WorkspaceSnapshotRef
from agintor.repositories.workspaces import (
    RepositoryMaterializationPolicy,
    RepositorySnapshotError,
    copy_repository_snapshot,
    materialize_task_workspace,
    repository_snapshot_digest,
    unified_diff_between,
)


def _snapshot(root: Path) -> WorkspaceSnapshotRef:
    return WorkspaceSnapshotRef(
        snapshot_id="snapshot.test",
        uri=str(root),
        digest=repository_snapshot_digest(root),
        format="directory",
    )


def test_materialization_preserves_source_and_separates_base_from_working_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(source)

    workspace = materialize_task_workspace(snapshot, tmp_path / "run")
    (workspace.working_root / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace.immutable_base_root / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert repository_snapshot_digest(source) == snapshot.digest
    patch = unified_diff_between(
        workspace.immutable_base_root,
        workspace.working_root,
        max_patch_bytes=4096,
    )
    assert "--- a/src/app.py" in patch
    assert "+++ b/src/app.py" in patch
    assert "-VALUE = 1" in patch
    assert "+VALUE = 2" in patch


def test_materialization_rejects_digest_mismatch_and_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.py").write_text("x = 1\n", encoding="utf-8")
    bad = _snapshot(source).model_copy(update={"digest": "0" * 64})

    with pytest.raises(RepositorySnapshotError, match="digest"):
        materialize_task_workspace(bad, tmp_path / "run")

    workspace = materialize_task_workspace(_snapshot(source), tmp_path / "run")
    assert workspace.working_root.is_dir()
    with pytest.raises(RepositorySnapshotError, match="already"):
        materialize_task_workspace(_snapshot(source), tmp_path / "run")


def test_snapshot_rejects_symlinks(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "target.py"
    target.write_text("x = 1\n", encoding="utf-8")
    link = source / "link.py"
    link.write_text("simulated link entry\n", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == link or original_is_symlink(path),
    )

    with pytest.raises(RepositorySnapshotError, match="symlinks"):
        repository_snapshot_digest(source)


def test_snapshot_digest_and_copy_reject_junctions_before_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text("x = 1\n", encoding="utf-8")
    junction = source / "junction"
    junction.mkdir()
    (junction / "external.py").write_text("raise AssertionError\n", encoding="utf-8")
    original_is_junction = getattr(Path, "is_junction", lambda _path: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path == junction or original_is_junction(path),
        raising=False,
    )

    with pytest.raises(RepositorySnapshotError, match="symlinks or junctions"):
        repository_snapshot_digest(source)
    destination = tmp_path / "copy"
    with pytest.raises(RepositorySnapshotError, match="symlinks or junctions"):
        copy_repository_snapshot(source, destination)
    assert not destination.exists()


def test_snapshot_source_and_copy_destination_reject_linked_parent_components(
    tmp_path: Path,
    monkeypatch,
) -> None:
    linked_parent = tmp_path / "linked-parent"
    linked_parent.mkdir()
    source_under_link = linked_parent / "source"
    source_under_link.mkdir()
    (source_under_link / "file.py").write_text("x = 1\n", encoding="utf-8")
    safe_source = tmp_path / "safe-source"
    safe_source.mkdir()
    (safe_source / "file.py").write_text("x = 1\n", encoding="utf-8")
    original_is_junction = getattr(Path, "is_junction", lambda _path: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path == linked_parent or original_is_junction(path),
        raising=False,
    )

    with pytest.raises(RepositorySnapshotError, match="may not cross"):
        repository_snapshot_digest(source_under_link)
    unsafe_destination = linked_parent / "copy"
    with pytest.raises(RepositorySnapshotError, match="may not cross"):
        copy_repository_snapshot(safe_source, unsafe_destination)
    assert not unsafe_destination.exists()


def test_snapshot_rejects_resource_overruns(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text("x = 1\n", encoding="utf-8")
    (source / "other.py").write_text("y = 2\n", encoding="utf-8")

    with pytest.raises(RepositorySnapshotError, match="max_files"):
        repository_snapshot_digest(
            source,
            policy=RepositoryMaterializationPolicy(max_files=1),
        )


def test_diff_rejects_patch_overrun_and_binary_content(tmp_path: Path) -> None:
    base = tmp_path / "base"
    working = tmp_path / "working"
    base.mkdir()
    working.mkdir()
    (base / "file.py").write_text("x = 1\n", encoding="utf-8")
    (working / "file.py").write_text("x = 2\n", encoding="utf-8")

    with pytest.raises(RepositorySnapshotError, match="max_patch_bytes"):
        unified_diff_between(base, working, max_patch_bytes=1)

    # Unchanged binary assets are permitted; only mutations are rejected.
    (base / "asset.bin").write_bytes(b"\x00\x01")
    (working / "asset.bin").write_bytes(b"\x00\x01")
    assert "file.py" in unified_diff_between(base, working, max_patch_bytes=4096)

    (working / "binary.bin").write_bytes(b"\x00\x01")
    with pytest.raises(RepositorySnapshotError, match="binary"):
        unified_diff_between(base, working, max_patch_bytes=4096)
