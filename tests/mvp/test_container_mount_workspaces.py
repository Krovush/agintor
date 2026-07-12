from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from agintor.isolation.workspaces import (
    ContainerMountWorkspaceError,
    prepare_container_mount_tree,
    private_container_mount_workspace,
)


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX bind-mount accessibility is represented by Unix mode bits",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def test_private_parent_contains_world_accessible_mount_copy_and_cleans_up(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    regular = source / "nested" / "regular.txt"
    executable = source / "run.sh"
    regular.write_text("private input\n", encoding="utf-8")
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source.chmod(0o700)
    (source / "nested").chmod(0o700)
    regular.chmod(0o600)
    executable.chmod(0o700)
    source_modes = {
        path: _mode(path)
        for path in (source, source / "nested", regular, executable)
    }

    with private_container_mount_workspace(
        prefix="agintor-permissions-",
        parent=tmp_path,
    ) as mount_root:
        private_root = mount_root.parent
        shutil.copytree(source, mount_root)
        prepare_container_mount_tree(mount_root)

        assert _mode(private_root) == 0o700
        assert _mode(mount_root) == 0o777
        assert _mode(mount_root / "nested") == 0o777
        assert _mode(mount_root / "nested" / "regular.txt") == 0o666
        assert _mode(mount_root / "run.sh") == 0o777

    assert not private_root.exists()
    assert {path: _mode(path) for path in source_modes} == source_modes


def test_mount_preparation_rejects_links_without_touching_their_targets(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("outside\n", encoding="utf-8")
    target.chmod(0o600)

    with private_container_mount_workspace(
        prefix="agintor-link-rejection-",
        parent=tmp_path,
    ) as mount_root:
        private_root = mount_root.parent
        mount_root.mkdir()
        (mount_root / "escape").symlink_to(target)

        with pytest.raises(ContainerMountWorkspaceError, match="link or junction"):
            prepare_container_mount_tree(mount_root)

    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == "outside\n"
    assert not private_root.exists()


def test_mount_preparation_rejects_special_files_and_still_cleans_up(
    tmp_path: Path,
) -> None:
    with private_container_mount_workspace(
        prefix="agintor-special-rejection-",
        parent=tmp_path,
    ) as mount_root:
        private_root = mount_root.parent
        mount_root.mkdir()
        os.mkfifo(mount_root / "candidate.fifo")

        with pytest.raises(ContainerMountWorkspaceError, match="special file"):
            prepare_container_mount_tree(mount_root)

    assert not private_root.exists()
