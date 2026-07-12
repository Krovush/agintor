from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


_SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ContainerMountWorkspaceError(RuntimeError):
    """A disposable bind-mount workspace could not be prepared safely."""


@contextmanager
def private_container_mount_workspace(
    *,
    prefix: str,
    parent: str | Path | None = None,
) -> Iterator[Path]:
    """Yield a reserved bind-mount child beneath a host-private temp root.

    The yielded path does not exist yet so callers may materialize a repository
    directly into it.  On POSIX, the containing temp root stays mode ``0700``;
    only the child tree passed to :func:`prepare_container_mount_tree` becomes
    accessible to the configured non-root container user.
    """

    if not _SAFE_PREFIX_RE.fullmatch(prefix):
        raise ContainerMountWorkspaceError("container workspace prefix is unsafe")
    resolved_parent = _resolved_temp_parent(parent)
    private_root = Path(
        tempfile.mkdtemp(prefix=prefix, dir=str(resolved_parent))
    ).absolute()
    mount_root = private_root / "mount"
    try:
        _require_plain_directory(private_root, "private container workspace root")
        if private_root.parent != resolved_parent:
            raise ContainerMountWorkspaceError(
                "private container workspace escaped its selected parent"
            )
        if os.name == "posix":
            private_root.chmod(0o700)
        yield mount_root
    finally:
        _remove_private_workspace(private_root, resolved_parent)


def prepare_container_mount_tree(root: str | Path) -> None:
    """Make one disposable tree usable by an arbitrary numeric container user.

    Directories become ``0777`` and regular files become ``0666``.  If a file
    had any executable bit, all three executable bits are retained so scripts
    remain runnable by the frozen non-root uid.  Links, junctions, and special
    files fail closed and are never traversed.
    """

    mount_root = Path(root).absolute()
    _require_plain_directory(mount_root, "container mount root")
    entries = list(_walk_plain_tree(mount_root))
    if os.name != "posix":
        return
    for path, kind in entries:
        if kind == "directory":
            path.chmod(0o777)
            continue
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        executable = 0o111 if mode & 0o111 else 0
        path.chmod(0o666 | executable, follow_symlinks=False)
    mount_root.chmod(0o777)


def _resolved_temp_parent(parent: str | Path | None) -> Path:
    candidate = Path(tempfile.gettempdir()) if parent is None else Path(parent)
    resolved = candidate.expanduser().resolve()
    _require_plain_directory(resolved, "container workspace parent")
    return resolved


def _walk_plain_tree(root: Path) -> Iterator[tuple[Path, str]]:
    try:
        with os.scandir(root) as scanner:
            entries = sorted(scanner, key=lambda entry: entry.name)
    except OSError as exc:
        raise ContainerMountWorkspaceError(
            "container mount tree cannot be scanned"
        ) from exc
    for entry in entries:
        path = Path(entry.path)
        if _unsafe_link_kind(path) is not None:
            raise ContainerMountWorkspaceError(
                "container mount tree contains a link or junction"
            )
        try:
            if entry.is_dir(follow_symlinks=False):
                yield path, "directory"
                yield from _walk_plain_tree(path)
            elif entry.is_file(follow_symlinks=False):
                yield path, "file"
            else:
                raise ContainerMountWorkspaceError(
                    "container mount tree contains a special file"
                )
        except OSError as exc:
            raise ContainerMountWorkspaceError(
                "container mount tree entry cannot be inspected"
            ) from exc


def _unsafe_link_kind(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return "symlink"
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return "junction"
    except OSError as exc:
        raise ContainerMountWorkspaceError(
            "container workspace path cannot be inspected"
        ) from exc
    return None


def _require_plain_directory(path: Path, label: str) -> None:
    if _unsafe_link_kind(path) is not None:
        raise ContainerMountWorkspaceError(f"{label} may not be a link or junction")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise ContainerMountWorkspaceError(f"{label} cannot be inspected") from exc
    if not stat.S_ISDIR(mode):
        raise ContainerMountWorkspaceError(f"{label} must be a directory")


def _remove_private_workspace(private_root: Path, parent: Path) -> None:
    if not private_root.exists() and not private_root.is_symlink():
        return
    if private_root.parent != parent or _unsafe_link_kind(private_root) is not None:
        raise ContainerMountWorkspaceError(
            "container workspace cleanup refused an unexpected target"
        )
    try:
        shutil.rmtree(private_root)
    except OSError as exc:
        raise ContainerMountWorkspaceError(
            "container workspace cleanup failed"
        ) from exc


__all__ = [
    "ContainerMountWorkspaceError",
    "prepare_container_mount_tree",
    "private_container_mount_workspace",
]
