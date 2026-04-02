from __future__ import annotations

import os
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


def default_sandbox_cache_root() -> Path:
    env_root = str(os.environ.get("AGINTOR_SANDBOX_CACHE_ROOT", "")).strip()
    if env_root:
        return ensure_directory(Path(env_root))
    return ensure_directory(Path(tempfile.gettempdir()) / "agintor" / "sandbox_cache")


@dataclass(frozen=True)
class ArtifactPolicy:
    mode: ArtifactMode
    sandbox_root: Path

    @classmethod
    def resolve(
        cls,
        *,
        artifact_mode: str | ArtifactMode | None = None,
        retain_artifacts: bool | None = None,
        sandbox_root: Path | None = None,
        cache_namespace: str | None = None,
    ) -> "ArtifactPolicy":
        mode = parse_artifact_mode(artifact_mode, retain_artifacts=retain_artifacts)
        root = Path(sandbox_root) if sandbox_root is not None else default_sandbox_cache_root()
        if cache_namespace:
            root = ensure_directory(root / stable_hash(cache_namespace)[:12])
        return cls(mode=mode, sandbox_root=root)

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
