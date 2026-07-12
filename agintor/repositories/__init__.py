"""Immutable repository snapshot and scratch-workspace primitives."""

from .workspaces import (
    RepositoryMaterializationPolicy,
    RepositorySnapshotError,
    TaskWorkspace,
    materialize_task_workspace,
    repository_snapshot_digest,
    unified_diff_between,
)

__all__ = [
    "RepositoryMaterializationPolicy",
    "RepositorySnapshotError",
    "TaskWorkspace",
    "materialize_task_workspace",
    "repository_snapshot_digest",
    "unified_diff_between",
]
