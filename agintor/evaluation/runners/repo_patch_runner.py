from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...utils import stable_hash

_INHERITED_ENV_ALLOWLIST = {
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}


class RepoPatchRunnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoPatchCommand(RepoPatchRunnerModel):
    name: str = ""
    command: str | list[str]
    timeout_s: float = 30.0
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fill_name(self) -> "RepoPatchCommand":
        if not self.name:
            command = self.command if isinstance(self.command, str) else " ".join(self.command)
            self.name = command[:80]
        return self


class RepoPatchFixture(RepoPatchRunnerModel):
    repo_snapshot_path: str
    public_test_commands: list[RepoPatchCommand] = Field(default_factory=list)
    sealed_test_commands: list[RepoPatchCommand] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=lambda: ["tests"])
    command_env: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 30.0

    @model_validator(mode="after")
    def validate_fixture(self) -> "RepoPatchFixture":
        root = Path(self.repo_snapshot_path)
        if not root.is_dir():
            raise ValueError(f"repo snapshot path is not a directory: {root}")
        if not self.public_test_commands and not self.sealed_test_commands:
            raise ValueError("repo_patch fixture requires at least one public or sealed test command")
        self.public_test_commands = [_normalize_command(command, default_timeout_s=self.timeout_s) for command in self.public_test_commands]
        self.sealed_test_commands = [_normalize_command(command, default_timeout_s=self.timeout_s) for command in self.sealed_test_commands]
        return self

    @classmethod
    def from_spec_inputs(cls, inputs: Mapping[str, Any]) -> "RepoPatchFixture | None":
        path = str(inputs.get("repo_snapshot_path", "") or "").strip()
        if not path:
            return None
        sealed = inputs.get("sealed_test_commands", inputs.get("hidden_test_commands", []))
        timeout_s = float(inputs.get("timeout_s", 30.0) or 30.0)
        return cls(
            repo_snapshot_path=path,
            public_test_commands=[
                _normalize_command(command, default_timeout_s=timeout_s)
                for command in _as_list(inputs.get("public_test_commands", []))
            ],
            sealed_test_commands=[
                _normalize_command(command, default_timeout_s=timeout_s)
                for command in _as_list(sealed)
            ],
            protected_paths=[str(path) for path in _as_list(inputs.get("protected_paths", ["tests"]))],
            command_env={str(key): str(value) for key, value in dict(inputs.get("command_env", {}) or {}).items()},
            timeout_s=timeout_s,
        )


class RepoPatchCommandResult(RepoPatchRunnerModel):
    name: str
    command: str | list[str]
    command_digest: str
    exit_code: int
    stdout_digest: str
    stderr_digest: str
    log_digest: str
    duration_s: float
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""


class RepoPatchRunnerResult(RepoPatchRunnerModel):
    runner_id: str = "repo_patch_runner.v1"
    runner_digest: str = ""
    status: str
    applied: bool
    public_tests_passed: bool
    hidden_tests_passed: bool | None = None
    tampered_tests: bool = False
    tampered_paths: list[str] = Field(default_factory=list)
    repo_snapshot_digest: str
    public_test_command_digest: str
    hidden_tests_digest: str
    environment_digest: str
    fixture_digest: str
    clean_copy_digest_before: str
    clean_copy_digest_after: str = ""
    patch_digest: str = ""
    patch_apply: RepoPatchCommandResult | None = None
    public_command_results: list[RepoPatchCommandResult] = Field(default_factory=list)
    hidden_command_results: list[RepoPatchCommandResult] = Field(default_factory=list)
    observations: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fill_runner_digest(self) -> "RepoPatchRunnerResult":
        if not self.runner_digest:
            self.runner_digest = stable_hash(
                "repo_patch_runner.v1",
                self.status,
                self.applied,
                self.public_tests_passed,
                self.hidden_tests_passed,
                self.tampered_tests,
                self.tampered_paths,
                self.repo_snapshot_digest,
                self.public_test_command_digest,
                self.hidden_tests_digest,
                self.environment_digest,
                self.fixture_digest,
                self.clean_copy_digest_before,
                self.clean_copy_digest_after,
                self.patch_digest,
                self.patch_apply.model_dump(mode="json") if self.patch_apply else {},
                [result.model_dump(mode="json") for result in self.public_command_results],
                [result.model_dump(mode="json") for result in self.hidden_command_results],
            )
        return self


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_command(command: Any, *, default_timeout_s: float = 30.0) -> RepoPatchCommand:
    if isinstance(command, RepoPatchCommand):
        return command
    if isinstance(command, str):
        return RepoPatchCommand(command=command, timeout_s=default_timeout_s)
    if isinstance(command, Sequence) and not isinstance(command, (bytes, bytearray, str)):
        return RepoPatchCommand(command=[str(part) for part in command], timeout_s=default_timeout_s)
    if isinstance(command, Mapping):
        payload = dict(command)
        if "timeout_s" not in payload:
            payload["timeout_s"] = default_timeout_s
        return RepoPatchCommand.model_validate(payload)
    raise ValueError(f"unsupported repo_patch command shape: {command!r}")


def command_suite_digest(commands: Sequence[RepoPatchCommand | Mapping[str, Any] | str | Sequence[str]]) -> str:
    normalized = [_normalize_command(command).model_dump(mode="json", exclude_none=True) for command in commands]
    return stable_hash("repo_patch.command_suite", normalized)


def repo_snapshot_digest(repo_root: str | Path) -> str:
    root = Path(repo_root)
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _ignored_path(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        rows.append({"path": rel, "digest": stable_hash(path.read_bytes())})
    return stable_hash("repo_patch.repo_snapshot", rows)


def protected_path_digest(repo_root: str | Path, protected_paths: Sequence[str]) -> dict[str, str]:
    root = Path(repo_root).resolve()
    digests: dict[str, str] = {}
    for raw_path in protected_paths:
        rel = str(raw_path or "").strip().replace("\\", "/")
        if not rel:
            continue
        path = (root / rel).resolve()
        if not _path_within(path, root) or not path.exists():
            digests[rel] = ""
        elif path.is_file():
            digests[rel] = stable_hash(path.read_bytes())
        else:
            digests[rel] = repo_snapshot_digest(path)
    return dict(sorted(digests.items()))


def repo_patch_fixture_digest(fixture: RepoPatchFixture) -> str:
    return stable_hash(
        "repo_patch.fixture",
        repo_snapshot_digest(fixture.repo_snapshot_path),
        command_suite_digest(fixture.public_test_commands),
        command_suite_digest(fixture.sealed_test_commands),
        protected_path_digest(fixture.repo_snapshot_path, fixture.protected_paths),
        environment_digest(fixture),
    )


def environment_digest(fixture: RepoPatchFixture) -> str:
    return stable_hash(
        "repo_patch.environment",
        {
            "python": sys.version,
            "platform": platform.platform(),
            "commands": [
                {
                    "name": command.name,
                    "env": _effective_command_env(fixture, command),
                }
                for command in [*fixture.public_test_commands, *fixture.sealed_test_commands]
            ],
        },
    )


class RepoPatchEvaluatorRunner:
    def run(
        self,
        *,
        candidate_artifact: Any,
        fixture: RepoPatchFixture,
    ) -> RepoPatchRunnerResult:
        snapshot_root = Path(fixture.repo_snapshot_path).resolve()
        repo_digest = repo_snapshot_digest(snapshot_root)
        public_digest = command_suite_digest(fixture.public_test_commands)
        hidden_digest = command_suite_digest(fixture.sealed_test_commands)
        env_digest = environment_digest(fixture)
        fixture_digest = repo_patch_fixture_digest(fixture)
        patch_digest = stable_hash("repo_patch.candidate_artifact", _safe_payload(candidate_artifact))

        with tempfile.TemporaryDirectory(prefix="agintor_repo_patch_") as temp_dir:
            clean_root = Path(temp_dir) / "repo"
            shutil.copytree(snapshot_root, clean_root, ignore=_copy_ignore)
            clean_before = repo_snapshot_digest(clean_root)
            protected_before = protected_path_digest(clean_root, fixture.protected_paths)
            patch_apply = self._apply_candidate_patch(clean_root, candidate_artifact, patch_digest)
            protected_after_patch = protected_path_digest(clean_root, fixture.protected_paths)
            tampered_paths = _changed_protected_paths(protected_before, protected_after_patch)

            public_results: list[RepoPatchCommandResult] = []
            hidden_results: list[RepoPatchCommandResult] = []
            if patch_apply.exit_code == 0:
                public_results = [self._run_command(clean_root, command, fixture) for command in fixture.public_test_commands]
                hidden_results = [self._run_command(clean_root, command, fixture) for command in fixture.sealed_test_commands]
            protected_after_tests = protected_path_digest(clean_root, fixture.protected_paths)
            tampered_paths = sorted(set([*tampered_paths, *_changed_protected_paths(protected_before, protected_after_tests)]))
            clean_after = repo_snapshot_digest(clean_root)

        public_passed = not fixture.public_test_commands or all(result.exit_code == 0 and not result.timed_out for result in public_results)
        hidden_passed = None
        if fixture.sealed_test_commands:
            hidden_passed = bool(hidden_results) and all(result.exit_code == 0 and not result.timed_out for result in hidden_results)
        status = "quarantine" if tampered_paths else "pass" if patch_apply.exit_code == 0 and public_passed and (hidden_passed is not False) else "fail"
        return RepoPatchRunnerResult(
            status=status,
            applied=patch_apply.exit_code == 0,
            public_tests_passed=public_passed,
            hidden_tests_passed=hidden_passed,
            tampered_tests=bool(tampered_paths),
            tampered_paths=tampered_paths,
            repo_snapshot_digest=repo_digest,
            public_test_command_digest=public_digest,
            hidden_tests_digest=hidden_digest,
            environment_digest=env_digest,
            fixture_digest=fixture_digest,
            clean_copy_digest_before=clean_before,
            clean_copy_digest_after=clean_after,
            patch_digest=patch_digest,
            patch_apply=patch_apply,
            public_command_results=public_results,
            hidden_command_results=hidden_results,
            observations={
                "protected_before": protected_before,
                "protected_after_tests": protected_after_tests,
            },
        )

    def _apply_candidate_patch(self, repo_root: Path, candidate_artifact: Any, patch_digest: str) -> RepoPatchCommandResult:
        direct_files = _direct_file_updates(candidate_artifact, repo_root)
        if direct_files:
            start = time.perf_counter()
            try:
                for rel_path, content in direct_files:
                    target = _resolve_repo_path(repo_root, rel_path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
            except Exception as exc:
                duration = time.perf_counter() - start
                stderr = str(exc)
                return _command_result(
                    name="apply_candidate_files",
                    command=["direct_file_update", patch_digest],
                    exit_code=1,
                    stdout="",
                    stderr=stderr,
                    duration_s=duration,
                )
            return _command_result(
                name="apply_candidate_files",
                command=["direct_file_update", patch_digest],
                exit_code=0,
                stdout="",
                stderr="",
                duration_s=time.perf_counter() - start,
            )

        patch_text = "\n".join(text for text in _patch_texts(candidate_artifact, repo_root) if text.strip())
        if not patch_text.strip():
            return _command_result(
                name="apply_patch",
                command=["git", "apply", "--whitespace=nowarn", "-"],
                exit_code=1,
                stdout="",
                stderr="candidate artifact did not contain a patch",
                duration_s=0.0,
            )
        start = time.perf_counter()
        try:
            completed = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=str(repo_root),
                input=patch_text,
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )
            return _command_result(
                name="apply_patch",
                command=["git", "apply", "--whitespace=nowarn", "-"],
                exit_code=int(completed.returncode),
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_s=time.perf_counter() - start,
            )
        except Exception as exc:
            return _command_result(
                name="apply_patch",
                command=["git", "apply", "--whitespace=nowarn", "-"],
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_s=time.perf_counter() - start,
            )

    def _run_command(self, repo_root: Path, command: RepoPatchCommand, fixture: RepoPatchFixture) -> RepoPatchCommandResult:
        env = _effective_command_env(fixture, command)
        start = time.perf_counter()
        timed_out = False
        try:
            completed = subprocess.run(
                command.command,
                cwd=str(repo_root),
                env=env,
                shell=isinstance(command.command, str),
                capture_output=True,
                text=True,
                timeout=float(command.timeout_s or fixture.timeout_s or 30.0),
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr) or f"timed out after {command.timeout_s}s"
        duration = time.perf_counter() - start
        return _command_result(
            name=command.name,
            command=command.command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            timed_out=timed_out,
        )


def _command_result(
    *,
    name: str,
    command: str | list[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_s: float,
    timed_out: bool = False,
) -> RepoPatchCommandResult:
    return RepoPatchCommandResult(
        name=name,
        command=command,
        command_digest=stable_hash("repo_patch.command", name, command),
        exit_code=exit_code,
        stdout_digest=stable_hash(stdout),
        stderr_digest=stable_hash(stderr),
        log_digest=stable_hash(stdout, stderr),
        duration_s=duration_s,
        timed_out=timed_out,
        stdout=stdout[-4000:],
        stderr=stderr[-4000:],
    )


def _base_command_env() -> dict[str, str]:
    return {
        key: str(value)
        for key, value in os.environ.items()
        if key.upper() in _INHERITED_ENV_ALLOWLIST
    }


def _effective_command_env(fixture: RepoPatchFixture, command: RepoPatchCommand) -> dict[str, str]:
    env = _base_command_env()
    env.update({str(key): str(value) for key, value in fixture.command_env.items()})
    env.update({str(key): str(value) for key, value in command.env.items()})
    return dict(sorted(env.items()))


def _safe_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _safe_payload(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_safe_payload(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {".git", "__pycache__", ".pytest_cache"}
    return {name for name in names if name in ignored}


def _ignored_path(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return any(part in {".git", "__pycache__", ".pytest_cache"} for part in parts)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_repo_path(root: Path, rel_path: str) -> Path:
    normalized = str(rel_path or "").replace("\\", "/").lstrip("/")
    target = (root / normalized).resolve()
    if not _path_within(target, root.resolve()):
        raise ValueError(f"repo_patch attempted to write outside clean copy: {rel_path!r}")
    return target


def _patch_texts(candidate_artifact: Any, repo_root: Path) -> list[str]:
    if isinstance(candidate_artifact, str):
        return [candidate_artifact]
    if not isinstance(candidate_artifact, Mapping):
        return []
    texts = []
    for key in ("patch", "diff"):
        value = candidate_artifact.get(key)
        if isinstance(value, str):
            texts.append(value)
    for key in ("updated_files", "files"):
        rows = candidate_artifact.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, Mapping) and isinstance(row.get("diff"), str):
                texts.append(_normalize_diff_paths(str(row["diff"]), _row_relative_path(row, repo_root)))
    return texts


def _direct_file_updates(candidate_artifact: Any, repo_root: Path) -> list[tuple[str, str]]:
    if not isinstance(candidate_artifact, Mapping):
        return []
    updates: list[tuple[str, str]] = []
    for key in ("files", "updated_files"):
        rows = candidate_artifact.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            rel_path = _row_relative_path(row, repo_root)
            if not rel_path or "updated_content" not in row:
                continue
            updates.append((rel_path, str(row.get("updated_content", ""))))
    return updates


def _row_relative_path(row: Mapping[str, Any], repo_root: Path) -> str:
    for key in ("relative_path", "repo_relative_path", "path"):
        rel_path = _repo_relative_path(row.get(key), repo_root)
        if rel_path:
            return rel_path
    return ""


def _repo_relative_path(value: Any, repo_root: Path) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("\\", "/")
    if not _looks_absolute_path(normalized):
        return normalized.lstrip("/")
    root = repo_root.resolve()
    try:
        path = Path(raw).resolve()
        return path.relative_to(root).as_posix()
    except Exception:
        pass
    target_parts = [part for part in normalized.split("/") if part and not part.endswith(":")]
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _ignored_path(path, root):
            continue
        rel = path.relative_to(root)
        rel_parts = list(rel.parts)
        if len(target_parts) >= len(rel_parts) and target_parts[-len(rel_parts):] == rel_parts:
            return rel.as_posix()
    return normalized.lstrip("/")


def _looks_absolute_path(path: str) -> bool:
    return path.startswith("/") or (len(path) > 2 and path[1] == ":" and path[2] == "/")


def _normalize_diff_paths(diff_text: str, rel_path: str) -> str:
    if not rel_path:
        return diff_text
    lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            lines.append(f"diff --git a/{rel_path} b/{rel_path}")
        elif line.startswith("--- ") and not _diff_header_is_dev_null(line):
            lines.append(f"--- a/{rel_path}")
        elif line.startswith("+++ ") and not _diff_header_is_dev_null(line):
            lines.append(f"+++ b/{rel_path}")
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if diff_text.endswith("\n") else "")


def _diff_header_is_dev_null(line: str) -> bool:
    path = line[4:].strip().split("\t", 1)[0].split(" ", 1)[0]
    return path == "/dev/null"


def _changed_protected_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if str(before.get(key, "")) != str(after.get(key, ""))]


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "RepoPatchCommand",
    "RepoPatchCommandResult",
    "RepoPatchEvaluatorRunner",
    "RepoPatchFixture",
    "RepoPatchRunnerResult",
    "command_suite_digest",
    "environment_digest",
    "protected_path_digest",
    "repo_patch_fixture_digest",
    "repo_snapshot_digest",
]
