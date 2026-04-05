from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from .utils import ensure_directory, stable_hash


class ArtifactMode(str, Enum):
    NONE = "none"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


class WorkspaceOrigin(str, Enum):
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


_TIMESTAMPED_SUBFOLDER_FORMAT = "%Y%m%d_%H%M%S"


def _local_wall_time(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now().astimezone().replace(tzinfo=None)
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def format_timestamped_subfolder_name(
    *,
    prefix: str = "run",
    when: datetime | None = None,
) -> str:
    stamp = _local_wall_time(when)
    return f"{prefix}_{stamp.strftime(_TIMESTAMPED_SUBFOLDER_FORMAT)}"


def parse_timestamped_subfolder_name(
    name: str,
    *,
    prefix: str = "run",
) -> datetime | None:
    expected_prefix = f"{prefix}_"
    if not name.startswith(expected_prefix):
        return None
    try:
        return datetime.strptime(name[len(expected_prefix) :], _TIMESTAMPED_SUBFOLDER_FORMAT)
    except ValueError:
        return None


def find_recent_timestamped_subfolder(
    root: Path,
    *,
    prefix: str = "run",
    within: timedelta = timedelta(hours=1),
    now: datetime | None = None,
) -> Path | None:
    root = Path(root)
    if not root.exists():
        return None
    current = _local_wall_time(now)
    newest: tuple[datetime, Path] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        stamp = parse_timestamped_subfolder_name(child.name, prefix=prefix)
        if stamp is None:
            continue
        age = current - stamp
        if age < timedelta(0) or age > within:
            continue
        if newest is None or stamp > newest[0]:
            newest = (stamp, child)
    return newest[1] if newest is not None else None


def resolve_recent_timestamped_subfolder(
    root: Path,
    *,
    prefix: str = "run",
    within: timedelta = timedelta(hours=1),
    now: datetime | None = None,
    create: bool = False,
) -> Path | None:
    recent = find_recent_timestamped_subfolder(root, prefix=prefix, within=within, now=now)
    if recent is not None or not create:
        return recent
    return ensure_directory(Path(root) / format_timestamped_subfolder_name(prefix=prefix, when=now))


def parse_artifact_mode(
    value: str | ArtifactMode | None = None,
    *,
    retain_artifacts: bool | None = None,
) -> ArtifactMode:
    if isinstance(value, ArtifactMode):
        return value
    text = str(value or os.environ.get("AGINTOR_ARTIFACT_MODE", "")).strip().lower()
    if not text:
        if retain_artifacts is not None:
            return ArtifactMode.ALWAYS if retain_artifacts else ArtifactMode.NONE
        return ArtifactMode.NONE
    if text in {"none", "off"}:
        return ArtifactMode.NONE
    if text in {"on_failure", "failure", "fail"}:
        return ArtifactMode.ON_FAILURE
    if text in {"always", "on", "keep"}:
        return ArtifactMode.ALWAYS
    raise ValueError(f"unknown artifact mode {value!r}")


def default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_artifact_root() -> Path:
    env_root = str(os.environ.get("AGINTOR_ARTIFACT_ROOT", "")).strip()
    if env_root:
        return Path(env_root)
    return Path(tempfile.gettempdir()) / "agintor" / "artifacts"


def default_sandbox_cache_root() -> Path:
    env_root = str(os.environ.get("AGINTOR_SANDBOX_CACHE_ROOT", "")).strip()
    if env_root:
        return ensure_directory(Path(env_root))
    return ensure_directory(Path(tempfile.gettempdir()) / "agintor" / "sandbox_cache")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass
class WorkspaceLease:
    path: Path
    purpose: str
    origin: WorkspaceOrigin
    cleanup_on_release: bool = False

    def ensure(self) -> Path:
        return ensure_directory(self.path)

    def child_path(self, *parts: str) -> Path:
        return self.path.joinpath(*parts)

    def ensure_child_dir(self, *parts: str) -> Path:
        return ensure_directory(self.child_path(*parts))

    def release(self) -> None:
        if not self.cleanup_on_release:
            return
        shutil.rmtree(self.path, ignore_errors=True)


@dataclass(frozen=True)
class ArtifactAllocator:
    repo_root: Path
    artifact_root: Path

    @classmethod
    def resolve(
        cls,
        *,
        repo_root: Path | None = None,
        artifact_root: Path | None = None,
    ) -> "ArtifactAllocator":
        return cls(
            repo_root=Path(repo_root) if repo_root is not None else default_repo_root(),
            artifact_root=Path(artifact_root) if artifact_root is not None else default_artifact_root(),
        )

    def _assert_implicit_path_allowed(self, path: Path) -> None:
        if _is_within(path, self.repo_root):
            raise ValueError(
                f"implicit artifact path must not live inside repo root: path={path} repo_root={self.repo_root}"
            )

    def _purpose_root(self, purpose: str) -> Path:
        root = ensure_directory(self.artifact_root / purpose)
        self._assert_implicit_path_allowed(root)
        return root

    def explicit_workspace(self, path: str | Path, *, purpose: str) -> WorkspaceLease:
        return WorkspaceLease(
            path=Path(path),
            purpose=purpose,
            origin=WorkspaceOrigin.EXPLICIT,
            cleanup_on_release=False,
        )

    def implicit_workspace(self, *, purpose: str, prefix: str | None = None) -> WorkspaceLease:
        purpose_root = self._purpose_root(purpose)
        workspace = Path(
            tempfile.mkdtemp(
                prefix=f"agintor_{(prefix or purpose).strip('_')}_",
                dir=str(purpose_root),
            )
        )
        self._assert_implicit_path_allowed(workspace)
        return WorkspaceLease(
            path=workspace,
            purpose=purpose,
            origin=WorkspaceOrigin.IMPLICIT,
            cleanup_on_release=True,
        )

    def workspace(self, path: str | Path | None, *, purpose: str, prefix: str | None = None) -> WorkspaceLease:
        if path is not None and str(path).strip():
            return self.explicit_workspace(path, purpose=purpose)
        return self.implicit_workspace(purpose=purpose, prefix=prefix)

    def timestamped_bucket(
        self,
        *,
        purpose: str,
        prefix: str = "run",
        within: timedelta = timedelta(hours=1),
        now: datetime | None = None,
        create: bool = False,
    ) -> Path | None:
        root = self._purpose_root(purpose)
        return resolve_recent_timestamped_subfolder(
            root,
            prefix=prefix,
            within=within,
            now=now,
            create=create,
        )


@dataclass(frozen=True)
class ArtifactPolicy:
    mode: ArtifactMode
    repo_root: Path
    artifact_root: Path
    sandbox_root: Path

    @classmethod
    def resolve(
        cls,
        *,
        artifact_mode: str | ArtifactMode | None = None,
        retain_artifacts: bool | None = None,
        repo_root: Path | None = None,
        artifact_root: Path | None = None,
        sandbox_root: Path | None = None,
        cache_namespace: str | None = None,
    ) -> "ArtifactPolicy":
        mode = parse_artifact_mode(artifact_mode, retain_artifacts=retain_artifacts)
        repo = Path(repo_root) if repo_root is not None else default_repo_root()
        artifact = Path(artifact_root) if artifact_root is not None else default_artifact_root()
        root = Path(sandbox_root) if sandbox_root is not None else default_sandbox_cache_root()
        if cache_namespace:
            root = ensure_directory(root / stable_hash(cache_namespace)[:12])
        return cls(mode=mode, repo_root=repo, artifact_root=artifact, sandbox_root=root)

    def allocator(self) -> ArtifactAllocator:
        return ArtifactAllocator.resolve(repo_root=self.repo_root, artifact_root=self.artifact_root)

    @property
    def keep_failures(self) -> bool:
        return self.mode in {ArtifactMode.ON_FAILURE, ArtifactMode.ALWAYS}

    @property
    def keep_successes(self) -> bool:
        return self.mode == ArtifactMode.ALWAYS

    @property
    def write_traces(self) -> bool:
        return self.mode in {ArtifactMode.ON_FAILURE, ArtifactMode.ALWAYS}

    @property
    def persist_tool_artifacts(self) -> bool:
        return self.mode == ArtifactMode.ALWAYS
