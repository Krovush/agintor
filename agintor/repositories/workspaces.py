from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from ..contracts.epochs import WorkspaceSnapshotRef
from ..core.identity import canonical_identity_digest


_IGNORED_PARTS = frozenset({".git", ".pytest_cache", "__pycache__", ".agintor"})


class RepositorySnapshotError(ValueError):
    pass


class RepositoryMaterializationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_files: int = Field(default=20_000, gt=0)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    max_single_file_bytes: int = Field(default=16 * 1024 * 1024, gt=0)


class TaskWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    snapshot_digest: str
    source_root: Path
    immutable_base_root: Path
    working_root: Path


def _path_is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if not callable(is_junction):
        return False
    try:
        return bool(is_junction())
    except OSError:
        return False


def _unsafe_link_kind(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    if _path_is_junction(path):
        return "junction"
    return None


def _reject_unsafe_link(path: Path, relative: Path) -> None:
    if _unsafe_link_kind(path) is not None:
        raise RepositorySnapshotError(
            "repository snapshots may not contain symlinks or junctions: "
            f"{relative.as_posix()}"
        )


def _absolute_without_resolving(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def _path_prefixes(path: Path) -> Iterable[Path]:
    absolute = _absolute_without_resolving(path)
    anchor = Path(absolute.anchor) if absolute.anchor else Path()
    current = anchor
    for part in absolute.parts[len(anchor.parts) :]:
        current = current / part
        yield current


def _reject_unsafe_path_components(path: Path, label: str) -> None:
    for component in _path_prefixes(path):
        if _unsafe_link_kind(component) is not None:
            raise RepositorySnapshotError(
                f"{label} may not cross symlinks or junctions: {component}"
            )


def _iter_snapshot_entries(root: Path) -> Iterable[tuple[Path, Path, str]]:
    candidate = root.expanduser()
    _reject_unsafe_path_components(candidate, "repository snapshot path")
    _reject_unsafe_link(candidate, Path("."))
    resolved = candidate.resolve()

    def walk(directory: Path) -> Iterable[tuple[Path, Path, str]]:
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            relative = directory.relative_to(resolved).as_posix()
            raise RepositorySnapshotError(
                f"repository snapshot directory cannot be scanned: {relative}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(resolved)
            if any(part in _IGNORED_PARTS for part in relative.parts):
                continue
            _reject_unsafe_link(path, relative)
            try:
                if entry.is_dir(follow_symlinks=False):
                    yield path, relative, "dir"
                    yield from walk(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path, relative, "file"
            except OSError as exc:
                raise RepositorySnapshotError(
                    f"repository snapshot entry cannot be inspected: {relative.as_posix()}"
                ) from exc

    yield from walk(resolved)


def _iter_snapshot_files(root: Path) -> Iterable[Path]:
    for path, _relative, kind in _iter_snapshot_entries(root):
        if kind == "file":
            yield path


def _snapshot_manifest(
    root: Path,
    *,
    policy: RepositoryMaterializationPolicy,
) -> list[dict[str, str | int]]:
    candidate = root.expanduser()
    _reject_unsafe_path_components(candidate, "repository snapshot path")
    _reject_unsafe_link(candidate, Path("."))
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise RepositorySnapshotError(f"repository snapshot is not a directory: {resolved}")
    manifest: list[dict[str, str | int]] = []
    total_bytes = 0
    for path in _iter_snapshot_files(resolved):
        size = path.stat().st_size
        if size > policy.max_single_file_bytes:
            raise RepositorySnapshotError(
                f"repository file exceeds max_single_file_bytes: {path.relative_to(resolved).as_posix()}"
            )
        total_bytes += size
        if total_bytes > policy.max_total_bytes:
            raise RepositorySnapshotError("repository snapshot exceeds max_total_bytes")
        manifest.append(
            {
                "path": path.relative_to(resolved).as_posix(),
                "size": size,
                "sha256": _file_digest(path),
            }
        )
        if len(manifest) > policy.max_files:
            raise RepositorySnapshotError("repository snapshot exceeds max_files")
    return manifest


def repository_snapshot_digest(
    root: str | Path,
    *,
    policy: RepositoryMaterializationPolicy | None = None,
) -> str:
    effective = policy or RepositoryMaterializationPolicy()
    return canonical_identity_digest(
        {
            "format": "agintor-directory-snapshot-v1",
            "files": _snapshot_manifest(Path(root), policy=effective),
        },
        domain="repository-snapshot",
    )


def resolve_local_snapshot_uri(
    uri: str,
    *,
    relative_to: str | Path | None = None,
) -> Path:
    raw = str(uri or "").strip()
    if not raw:
        raise RepositorySnapshotError("workspace snapshot URI may not be empty")
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme.casefold() != "file":
        # A Windows drive letter is parsed as a one-character scheme.
        if not (len(parsed.scheme) == 1 and len(raw) >= 3 and raw[1] == ":"):
            raise RepositorySnapshotError("MVP workspace snapshots must use local paths or file:// URIs")
    if parsed.scheme.casefold() == "file":
        path_text = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            raise RepositorySnapshotError("remote file:// authorities are not supported")
        if os.name == "nt" and path_text.startswith("/") and len(path_text) > 2 and path_text[2] == ":":
            path_text = path_text[1:]
        path = Path(path_text).expanduser()
        _reject_unsafe_path_components(path, "workspace snapshot URI")
        return path.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute() and relative_to is not None:
        path = Path(relative_to).expanduser() / path
    _reject_unsafe_path_components(path, "workspace snapshot URI")
    return path.resolve()


def materialize_task_workspace(
    snapshot: WorkspaceSnapshotRef,
    destination_root: str | Path,
    *,
    policy: RepositoryMaterializationPolicy | None = None,
    source_root: str | Path | None = None,
) -> TaskWorkspace:
    effective = policy or RepositoryMaterializationPolicy()
    resolved_source_root = (
        resolve_local_snapshot_uri(snapshot.uri)
        if source_root is None
        else resolve_local_snapshot_uri(str(source_root))
    )
    before = repository_snapshot_digest(resolved_source_root, policy=effective)
    if before != snapshot.digest:
        raise RepositorySnapshotError("workspace snapshot digest does not match its immutable reference")

    destination_candidate = Path(destination_root).expanduser()
    _reject_unsafe_path_components(destination_candidate, "task workspace destination")
    destination = destination_candidate.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    base_root = destination / "immutable_base"
    working_root = destination / "working"
    if base_root.exists() or working_root.exists():
        raise RepositorySnapshotError("task workspace destination already contains materialized roots")

    staging = destination / f".materializing-{uuid.uuid4().hex}"
    staging_base = staging / "immutable_base"
    staging_working = staging / "working"
    try:
        _copy_snapshot(resolved_source_root, staging_base)
        copied_digest = repository_snapshot_digest(staging_base, policy=effective)
        if copied_digest != before:
            raise RepositorySnapshotError("materialized immutable base digest differs from source snapshot")
        _copy_snapshot(staging_base, staging_working)
        if repository_snapshot_digest(staging_working, policy=effective) != before:
            raise RepositorySnapshotError("materialized working-copy digest differs before execution")
        if repository_snapshot_digest(resolved_source_root, policy=effective) != before:
            raise RepositorySnapshotError("source snapshot changed while it was being materialized")
        staging_base.replace(base_root)
        staging_working.replace(working_root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _make_tree_read_only(base_root)
    return TaskWorkspace(
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=before,
        source_root=resolved_source_root,
        immutable_base_root=base_root,
        working_root=working_root,
    )


def unified_diff_between(
    base_root: str | Path,
    working_root: str | Path,
    *,
    max_patch_bytes: int,
) -> str:
    base_candidate = Path(base_root).expanduser()
    working_candidate = Path(working_root).expanduser()
    _reject_unsafe_path_components(base_candidate, "diff base root")
    _reject_unsafe_path_components(working_candidate, "diff working root")
    base = base_candidate.resolve()
    working = working_candidate.resolve()
    if not base.is_dir() or not working.is_dir():
        raise RepositorySnapshotError("diff roots must both be directories")
    base_files = _file_bytes_map(base)
    working_files = _file_bytes_map(working)
    chunks: list[str] = []
    encoded_size = 0
    for relative in sorted(set(base_files) | set(working_files)):
        before_raw = base_files.get(relative, b"")
        after_raw = working_files.get(relative, b"")
        if before_raw == after_raw:
            continue
        before = _decode_changed_text(before_raw, relative).splitlines(keepends=True)
        after = _decode_changed_text(after_raw, relative).splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="\n",
            )
        )
        encoded_size += len(diff.encode("utf-8"))
        if encoded_size > max_patch_bytes:
            raise RepositorySnapshotError("generated patch exceeds max_patch_bytes")
        chunks.append(diff)
    return "".join(chunks)


def _copy_snapshot(source: Path, destination: Path) -> None:
    candidate = source.expanduser()
    _reject_unsafe_path_components(candidate, "snapshot copy source")
    _reject_unsafe_link(candidate, Path("."))
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise RepositorySnapshotError(f"repository snapshot is not a directory: {resolved}")
    target_candidate = destination.expanduser()
    _reject_unsafe_path_components(target_candidate.parent, "snapshot copy destination")
    if target_candidate.exists() or target_candidate.is_symlink():
        _reject_unsafe_path_components(target_candidate, "snapshot copy destination")
    target_root = target_candidate.resolve()
    if target_root.exists():
        raise RepositorySnapshotError("snapshot copy destination already exists")
    try:
        target_root.mkdir(parents=True)
        for path, relative, kind in _iter_snapshot_entries(resolved):
            target = target_root / relative
            if kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
            elif kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_unsafe_link(path, relative)
                shutil.copy2(path, target, follow_symlinks=False)
                _reject_unsafe_link(target, relative)
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        raise


def copy_repository_snapshot(source: str | Path, destination: str | Path) -> None:
    _copy_snapshot(Path(source), Path(destination))


def _make_tree_read_only(root: Path) -> None:
    for path in [*root.rglob("*"), root]:
        try:
            mode = path.stat().st_mode
            path.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
        except OSError as exc:
            raise RepositorySnapshotError(f"failed to make immutable base read-only: {path}") from exc


def _file_bytes_map(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in _iter_snapshot_files(root):
        relative = path.relative_to(root).as_posix()
        files[relative] = path.read_bytes()
    return files


def _decode_changed_text(raw: bytes, relative: str) -> str:
    if b"\x00" in raw:
        raise RepositorySnapshotError(f"binary file changes are not supported in MVP patches: {relative}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepositorySnapshotError(f"non-UTF-8 file changes are not supported: {relative}") from exc


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "RepositoryMaterializationPolicy",
    "RepositorySnapshotError",
    "TaskWorkspace",
    "copy_repository_snapshot",
    "materialize_task_workspace",
    "repository_snapshot_digest",
    "resolve_local_snapshot_uri",
    "unified_diff_between",
]
