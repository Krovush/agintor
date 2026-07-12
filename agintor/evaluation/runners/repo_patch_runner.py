from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...contracts.outcomes import OutcomeHealth
from ...isolation.commands import IsolatedCommandRequest, IsolatedCommandResult, IsolatedCommandStatus
from ...isolation.workspaces import (
    prepare_container_mount_tree,
    private_container_mount_workspace,
)
from ...repositories.workspaces import repository_snapshot_digest, resolve_local_snapshot_uri
from ...utils import stable_hash
from .repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    RepoPatchExecutionBackend,
    TrustedLocalRepoPatchCommandBackend,
)

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
    working_directory: str = "."
    timeout_s: float = Field(default=30.0, gt=0.0)
    expected_exit_codes: tuple[int, ...] = (0,)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        raw = str(value or ".").strip().replace("\\", "/") or "."
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("repo-patch command working_directory must stay within the scratch repository")
        return path.as_posix()

    @field_validator("expected_exit_codes")
    @classmethod
    def validate_expected_exit_codes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        normalized = tuple(int(code) for code in value)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("repo-patch command expected_exit_codes must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def fill_name(self) -> "RepoPatchCommand":
        if not self.name:
            command = self.command if isinstance(self.command, str) else " ".join(self.command)
            self.name = command[:80]
        return self


class RepoPatchFixture(RepoPatchRunnerModel):
    repo_snapshot_path: str
    expected_repo_snapshot_digest: str = ""
    authority_fixture_digest: str = ""
    evaluation_contract_digest: str = ""
    public_test_commands: list[RepoPatchCommand] = Field(default_factory=list)
    sealed_test_commands: list[RepoPatchCommand] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=lambda: ["tests"])
    command_env: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0.0)

    @field_validator(
        "expected_repo_snapshot_digest",
        "authority_fixture_digest",
        "evaluation_contract_digest",
    )
    @classmethod
    def validate_optional_digest(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized and not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("repo-patch fixture identity fields must be lowercase SHA-256 digests")
        return normalized

    @model_validator(mode="after")
    def validate_fixture(self) -> "RepoPatchFixture":
        root = Path(self.repo_snapshot_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"repo snapshot path is not a directory: {root}")
        if not self.public_test_commands and not self.sealed_test_commands:
            raise ValueError("repo_patch fixture requires at least one public or sealed test command")
        protected_paths: list[str] = []
        for raw_path in self.protected_paths:
            protected_paths.append(_validated_repo_relative_path(raw_path, label="protected path"))
        self.repo_snapshot_path = str(root)
        self.protected_paths = protected_paths
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
            expected_repo_snapshot_digest=str(inputs.get("repo_snapshot_digest", "") or ""),
            authority_fixture_digest=str(inputs.get("fixture_digest", "") or ""),
            evaluation_contract_digest=str(inputs.get("evaluation_contract_digest", "") or ""),
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

    @classmethod
    def from_evaluation_contract(
        cls,
        contract: Any,
        *,
        public_test_commands: Sequence[Any],
        timeout_s: float = 30.0,
    ) -> "RepoPatchFixture":
        """Adapt sealed evaluator authority without importing it in public roles."""

        sealed_fixture = contract.sealed_fixture
        hidden_commands = [
            RepoPatchCommand(
                name=str(check.check_id),
                command=list(check.argv),
                working_directory=str(check.cwd),
                timeout_s=float(check.timeout_ms) / 1000.0,
                expected_exit_codes=tuple(check.expected_exit_codes),
                env={str(name): str(value) for name, value in check.environment},
            )
            for check in contract.hidden_checks
        ]
        return cls(
            repo_snapshot_path=str(resolve_local_snapshot_uri(sealed_fixture.uri)),
            expected_repo_snapshot_digest=str(sealed_fixture.public_snapshot_digest),
            authority_fixture_digest=str(sealed_fixture.fixture_digest),
            evaluation_contract_digest=str(contract.evaluation_contract_digest),
            public_test_commands=[
                _normalize_authority_command(command, default_timeout_s=timeout_s)
                for command in public_test_commands
            ],
            sealed_test_commands=hidden_commands,
            protected_paths=[str(path) for path in contract.protected_paths],
            timeout_s=timeout_s,
        )


class RepoPatchWorkspaceIntegrityCheck(RepoPatchRunnerModel):
    phase: Literal["patch_apply", "public_check", "sealed_check"]
    command_name: str
    command_index: int = -1
    sealed: bool = False
    expected_digest: str
    before_digest: str
    after_digest: str
    digest_source: str
    matched: bool


class RepoPatchCommandResult(RepoPatchRunnerModel):
    name: str
    command: str | list[str]
    command_digest: str
    backend_id: str = ""
    terminal_status: str = IsolatedCommandStatus.COMPLETED.value
    exit_code: int
    expected_exit_codes: tuple[int, ...] = (0,)
    stdout_digest: str
    stderr_digest: str
    log_digest: str
    duration_s: float
    timed_out: bool = False
    output_truncated: bool = False
    failure_detail: str | None = None
    sealed: bool = False
    workspace_digest_before: str = ""
    workspace_digest_after: str = ""
    workspace_digest_source: str = ""
    stdout: str = ""
    stderr: str = ""


class RepoPatchRunnerResult(RepoPatchRunnerModel):
    runner_id: str = "repo_patch_runner.v1"
    runner_digest: str = ""
    execution_backend_id: str
    execution_backend_digest: str
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
    evaluation_contract_digest: str = ""
    source_snapshot_digest_after: str
    source_snapshot_unchanged: bool
    scratch_snapshot_matched: bool
    fixture_identity_matched: bool
    patched_clean_digest: str = ""
    clean_copy_snapshot_unchanged: bool = True
    workspace_drift_evidence: list[RepoPatchWorkspaceIntegrityCheck] = Field(default_factory=list)
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
                self.execution_backend_id,
                self.execution_backend_digest,
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
                self.evaluation_contract_digest,
                self.source_snapshot_digest_after,
                self.source_snapshot_unchanged,
                self.scratch_snapshot_matched,
                self.fixture_identity_matched,
                self.patched_clean_digest,
                self.clean_copy_snapshot_unchanged,
                [item.model_dump(mode="json") for item in self.workspace_drift_evidence],
                self.clean_copy_digest_before,
                self.clean_copy_digest_after,
                self.patch_digest,
                self.patch_apply.model_dump(mode="json") if self.patch_apply else {},
                [result.model_dump(mode="json") for result in self.public_command_results],
                [result.model_dump(mode="json") for result in self.hidden_command_results],
            )
        return self

    @property
    def complete_repair(self) -> bool:
        return bool(
            self.status == "pass"
            and self.applied
            and self.public_tests_passed
            and self.hidden_tests_passed is not False
            and not self.tampered_tests
            and self.source_snapshot_unchanged
            and self.scratch_snapshot_matched
            and self.fixture_identity_matched
            and self.clean_copy_snapshot_unchanged
        )

    def outcome_health(
        self,
        *,
        no_leakage: bool,
        accounting_complete: bool,
    ) -> OutcomeHealth:
        command_results = [
            *([self.patch_apply] if self.patch_apply is not None else []),
            *self.public_command_results,
            *self.hidden_command_results,
        ]
        backend_launched = all(
            result.terminal_status != IsolatedCommandStatus.LAUNCH_FAILED.value
            for result in command_results
        )
        return OutcomeHealth(
            process_integrity=backend_launched,
            no_leakage=bool(no_leakage),
            environment_integrity=bool(
                self.execution_backend_id == IsolatedRepoPatchCommandBackend.backend_id
                and self.fixture_identity_matched
            ),
            evaluator_integrity=bool(
                self.source_snapshot_unchanged
                and self.scratch_snapshot_matched
                and self.clean_copy_snapshot_unchanged
                and not self.tampered_tests
            ),
            accounting_complete=bool(accounting_complete),
        )

    def outcome_receipt_evidence(
        self,
        *,
        no_leakage: bool,
        accounting_complete: bool,
    ) -> dict[str, Any]:
        """Measured fields consumed by evaluator-owned ``issue_outcome_receipt``."""

        return {
            "evaluation_contract_digest": self.evaluation_contract_digest,
            "environment_digest": self.environment_digest,
            "patch_digest": self.patch_digest,
            "complete_repair": self.complete_repair,
            "health": self.outcome_health(
                no_leakage=no_leakage,
                accounting_complete=accounting_complete,
            ),
        }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_command(command: Any, *, default_timeout_s: float = 30.0) -> RepoPatchCommand:
    if isinstance(command, RepoPatchCommand):
        return command
    if hasattr(command, "model_dump"):
        return _normalize_authority_command(command, default_timeout_s=default_timeout_s)
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


def _normalize_authority_command(command: Any, *, default_timeout_s: float) -> RepoPatchCommand:
    payload = command.model_dump(mode="python") if hasattr(command, "model_dump") else dict(command)
    timeout_s = payload.get("timeout_s")
    if timeout_s is None and payload.get("timeout_ms") is not None:
        timeout_s = float(payload["timeout_ms"]) / 1000.0
    environment = payload.get("env", payload.get("environment", {}))
    if isinstance(environment, Sequence) and not isinstance(environment, (str, bytes, bytearray, Mapping)):
        environment = {str(name): str(value) for name, value in environment}
    return RepoPatchCommand(
        name=str(payload.get("name", payload.get("step_id", payload.get("check_id", ""))) or ""),
        command=payload.get("command", payload.get("argv", [])),
        working_directory=str(payload.get("working_directory", payload.get("cwd", ".")) or "."),
        timeout_s=float(timeout_s or default_timeout_s),
        expected_exit_codes=tuple(payload.get("expected_exit_codes", (0,)) or (0,)),
        env={str(key): str(value) for key, value in dict(environment or {}).items()},
    )


def command_suite_digest(commands: Sequence[RepoPatchCommand | Mapping[str, Any] | str | Sequence[str]]) -> str:
    normalized = [_normalize_command(command).model_dump(mode="json", exclude_none=True) for command in commands]
    return stable_hash("repo_patch.command_suite", normalized)


def repo_snapshot_digest(repo_root: str | Path) -> str:
    return repository_snapshot_digest(Path(repo_root))


def protected_path_digest(repo_root: str | Path, protected_paths: Sequence[str]) -> dict[str, str]:
    root = Path(repo_root).resolve()
    digests: dict[str, str] = {}
    for raw_path in protected_paths:
        rel = _validated_repo_relative_path(raw_path, label="protected path")
        path = root.joinpath(*PurePosixPath(rel).parts)
        if path.is_symlink():
            digests[rel] = stable_hash("repo_patch.symlink", os.readlink(path))
        elif not path.exists():
            digests[rel] = ""
        elif path.is_file():
            digests[rel] = stable_hash(path.read_bytes())
        else:
            digests[rel] = repo_snapshot_digest(path)
    return dict(sorted(digests.items()))


def repo_patch_fixture_digest(
    fixture: RepoPatchFixture,
    backend: RepoPatchExecutionBackend | None = None,
) -> str:
    return stable_hash(
        "repo_patch.fixture",
        repo_snapshot_digest(fixture.repo_snapshot_path),
        command_suite_digest(fixture.public_test_commands),
        command_suite_digest(fixture.sealed_test_commands),
        protected_path_digest(fixture.repo_snapshot_path, fixture.protected_paths),
        environment_digest(fixture, backend),
    )


def environment_digest(
    fixture: RepoPatchFixture,
    backend: RepoPatchExecutionBackend | None = None,
) -> str:
    selected_backend = backend or TrustedLocalRepoPatchCommandBackend()
    return stable_hash(
        "repo_patch.environment",
        {
            "backend_id": selected_backend.backend_id,
            "backend_identity": _safe_payload(selected_backend.identity_payload),
            "commands": [
                {
                    "name": command.name,
                    "env": _effective_command_env(fixture, command, backend=selected_backend),
                }
                for command in [*fixture.public_test_commands, *fixture.sealed_test_commands]
            ],
        },
    )


_DIRECT_FILE_APPLY_SCRIPT = """
import json
import re
import sys
from pathlib import Path, PurePosixPath

root = Path.cwd().resolve()
rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for row in rows:
    raw = str(row["path"]).replace("\\\\", "/")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or re.match(r"^[A-Za-z]:/", raw) or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"unsafe candidate path: {raw!r}")
    target = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"candidate path crosses symlink: {raw!r}")
    resolved = target.resolve(strict=False)
    resolved.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"candidate target is a symlink: {raw!r}")
    target.write_bytes(str(row["content"]).encode("utf-8"))
""".strip()


class RepoPatchEvaluatorRunner:
    """Applies and evaluates one candidate in an evaluator-owned clean copy."""

    def __init__(self, command_backend: RepoPatchExecutionBackend) -> None:
        if command_backend is None:
            raise TypeError(
                "RepoPatchEvaluatorRunner requires an explicit execution backend; "
                "use IsolatedRepoPatchCommandBackend for V1 or trusted_local() only for legacy tests"
            )
        self.command_backend = command_backend

    @classmethod
    def trusted_local(cls) -> "RepoPatchEvaluatorRunner":
        return cls(TrustedLocalRepoPatchCommandBackend())

    def run(
        self,
        *,
        candidate_artifact: Any,
        fixture: RepoPatchFixture,
    ) -> RepoPatchRunnerResult:
        self._validate_command_shapes(fixture)
        self._validate_fixture_authority(fixture)
        snapshot_root = Path(fixture.repo_snapshot_path).resolve()
        repo_digest = repo_snapshot_digest(snapshot_root)
        public_digest = command_suite_digest(fixture.public_test_commands)
        hidden_digest = command_suite_digest(fixture.sealed_test_commands)
        env_digest = environment_digest(fixture, self.command_backend)
        fixture_digest = repo_patch_fixture_digest(fixture, self.command_backend)
        fixture_identity_matched = bool(
            (not fixture.expected_repo_snapshot_digest or fixture.expected_repo_snapshot_digest == repo_digest)
            and (not fixture.authority_fixture_digest or fixture.authority_fixture_digest == fixture_digest)
        )
        backend_digest = stable_hash("repo_patch.execution_backend", _safe_payload(self.command_backend.identity_payload))
        patch_digest = stable_hash("repo_patch.candidate_artifact", _safe_payload(candidate_artifact))
        source_integrity_scans: list[str] = [repo_digest]

        with private_container_mount_workspace(
            prefix="agintor_repo_patch_"
        ) as evaluation_root:
            evaluation_root.mkdir()
            clean_root = evaluation_root / "repo"
            shutil.copytree(snapshot_root, clean_root, ignore=_copy_ignore, symlinks=True)
            clean_before = repo_snapshot_digest(clean_root)
            scratch_matched = clean_before == repo_digest
            protected_before = protected_path_digest(clean_root, fixture.protected_paths)
            public_results: list[RepoPatchCommandResult] = []
            hidden_results: list[RepoPatchCommandResult] = []
            tampered_paths: list[str] = []
            workspace_drift_evidence: list[RepoPatchWorkspaceIntegrityCheck] = []
            patched_clean_digest = ""

            if scratch_matched and fixture_identity_matched:
                patch_apply, rejected_protected = self._apply_candidate_patch(
                    evaluation_root,
                    clean_root,
                    candidate_artifact,
                    fixture,
                    patch_digest,
                )
                tampered_paths.extend(rejected_protected)
            else:
                patch_apply = self._failed_result(
                    name="apply_patch",
                    command=["<copy-integrity-check>"],
                    stderr=(
                        "fresh evaluator copy did not match the immutable source snapshot"
                        if not scratch_matched
                        else "sealed fixture identity did not match evaluator-owned source and policy"
                    ),
                )

            source_integrity_scans.append(repo_snapshot_digest(snapshot_root))
            protected_after_patch = protected_path_digest(clean_root, fixture.protected_paths)
            tampered_paths.extend(_changed_protected_paths(protected_before, protected_after_patch))
            if patch_apply.exit_code == 0:
                patched_clean_digest = patch_apply.workspace_digest_after or repo_snapshot_digest(clean_root)
                workspace_drift_evidence.append(
                    _workspace_integrity_check(
                        phase="patch_apply",
                        command_result=patch_apply,
                        command_index=-1,
                        expected_digest=patched_clean_digest,
                        compare_before=False,
                    )
                )

            source_unchanged_so_far = all(digest == repo_digest for digest in source_integrity_scans)
            if patch_apply.exit_code == 0 and not tampered_paths and source_unchanged_so_far:
                for command_index, command in enumerate(fixture.public_test_commands):
                    command_result = self._run_command(evaluation_root, clean_root, command, fixture, sealed=False)
                    public_results.append(command_result)
                    workspace_drift_evidence.append(
                        _workspace_integrity_check(
                            phase="public_check",
                            command_result=command_result,
                            command_index=command_index,
                            expected_digest=patched_clean_digest,
                        )
                    )
                    source_integrity_scans.append(repo_snapshot_digest(snapshot_root))
                    protected_now = protected_path_digest(clean_root, fixture.protected_paths)
                    tampered_paths.extend(_changed_protected_paths(protected_before, protected_now))
                    if (
                        tampered_paths
                        or source_integrity_scans[-1] != repo_digest
                        or _workspace_drift_detected(workspace_drift_evidence)
                    ):
                        break

            source_unchanged_so_far = all(digest == repo_digest for digest in source_integrity_scans)
            public_complete = len(public_results) == len(fixture.public_test_commands)
            if (
                patch_apply.exit_code == 0
                and public_complete
                and not tampered_paths
                and source_unchanged_so_far
                and not _workspace_drift_detected(workspace_drift_evidence)
            ):
                for command_index, command in enumerate(fixture.sealed_test_commands):
                    command_result = self._run_command(evaluation_root, clean_root, command, fixture, sealed=True)
                    hidden_results.append(command_result)
                    workspace_drift_evidence.append(
                        _workspace_integrity_check(
                            phase="sealed_check",
                            command_result=command_result,
                            command_index=command_index,
                            expected_digest=patched_clean_digest,
                        )
                    )
                    source_integrity_scans.append(repo_snapshot_digest(snapshot_root))
                    protected_now = protected_path_digest(clean_root, fixture.protected_paths)
                    tampered_paths.extend(_changed_protected_paths(protected_before, protected_now))
                    if (
                        tampered_paths
                        or source_integrity_scans[-1] != repo_digest
                        or _workspace_drift_detected(workspace_drift_evidence)
                    ):
                        break

            protected_after_tests = protected_path_digest(clean_root, fixture.protected_paths)
            tampered_paths.extend(_changed_protected_paths(protected_before, protected_after_tests))
            clean_after = repo_snapshot_digest(clean_root)
            source_after = repo_snapshot_digest(snapshot_root)
            source_integrity_scans.append(source_after)

        tampered_paths = sorted(set(tampered_paths))
        source_unchanged = all(digest == repo_digest for digest in source_integrity_scans)
        clean_copy_snapshot_unchanged = not _workspace_drift_detected(workspace_drift_evidence)
        public_passed = not fixture.public_test_commands or (
            len(public_results) == len(fixture.public_test_commands)
            and all(_command_succeeded(result) for result in public_results)
        )
        hidden_passed = None
        if fixture.sealed_test_commands:
            hidden_passed = (
                len(hidden_results) == len(fixture.sealed_test_commands)
                and all(_command_succeeded(result) for result in hidden_results)
            )
        integrity_failure = not scratch_matched or not source_unchanged or not fixture_identity_matched
        if tampered_paths or integrity_failure or not clean_copy_snapshot_unchanged:
            status = "quarantine"
        elif patch_apply.exit_code == 0 and public_passed and hidden_passed is not False:
            status = "pass"
        else:
            status = "fail"
        return RepoPatchRunnerResult(
            execution_backend_id=self.command_backend.backend_id,
            execution_backend_digest=backend_digest,
            status=status,
            applied=patch_apply.exit_code == 0,
            public_tests_passed=public_passed,
            hidden_tests_passed=hidden_passed,
            tampered_tests=bool(tampered_paths or not clean_copy_snapshot_unchanged),
            tampered_paths=tampered_paths,
            repo_snapshot_digest=repo_digest,
            public_test_command_digest=public_digest,
            hidden_tests_digest=hidden_digest,
            environment_digest=env_digest,
            fixture_digest=fixture_digest,
            evaluation_contract_digest=fixture.evaluation_contract_digest,
            source_snapshot_digest_after=source_after,
            source_snapshot_unchanged=source_unchanged,
            scratch_snapshot_matched=scratch_matched,
            fixture_identity_matched=fixture_identity_matched,
            patched_clean_digest=patched_clean_digest,
            clean_copy_snapshot_unchanged=clean_copy_snapshot_unchanged,
            workspace_drift_evidence=workspace_drift_evidence,
            clean_copy_digest_before=clean_before,
            clean_copy_digest_after=clean_after,
            patch_digest=patch_digest,
            patch_apply=patch_apply,
            public_command_results=public_results,
            hidden_command_results=hidden_results,
            observations={
                "protected_before": protected_before,
                "protected_after_patch": protected_after_patch,
                "protected_after_tests": protected_after_tests,
                "source_integrity_scan_digests": source_integrity_scans,
                "patched_clean_digest": patched_clean_digest,
                "workspace_drift_evidence": [
                    item.model_dump(mode="json") for item in workspace_drift_evidence
                ],
            },
        )

    def _validate_command_shapes(self, fixture: RepoPatchFixture) -> None:
        for command in [*fixture.public_test_commands, *fixture.sealed_test_commands]:
            self.command_backend.command_argv(command.command)

    def _validate_fixture_authority(self, fixture: RepoPatchFixture) -> None:
        if not self.command_backend.is_isolated:
            return
        missing = [
            field_name
            for field_name in (
                "expected_repo_snapshot_digest",
                "authority_fixture_digest",
                "evaluation_contract_digest",
            )
            if not getattr(fixture, field_name)
        ]
        if missing:
            raise ValueError(
                "isolated repo-patch evaluation requires evaluator-owned fixture authority: "
                f"{missing}"
            )

    def _apply_candidate_patch(
        self,
        evaluation_root: Path,
        repo_root: Path,
        candidate_artifact: Any,
        fixture: RepoPatchFixture,
        patch_digest: str,
    ) -> tuple[RepoPatchCommandResult, list[str]]:
        try:
            direct_files = _direct_file_updates(
                candidate_artifact,
                repo_root,
                allow_legacy_absolute=not self.command_backend.is_isolated,
            )
            if direct_files:
                normalized_files = [
                    {
                        "path": _validated_repo_relative_path(path, label="candidate file path"),
                        "content": content,
                    }
                    for path, content in direct_files
                ]
                protected = _candidate_protected_paths(
                    [str(row["path"]) for row in normalized_files],
                    fixture.protected_paths,
                )
                if protected:
                    return (
                        self._failed_result(
                            name="apply_candidate_files",
                            command=["<rejected-protected-update>", patch_digest],
                            stderr="candidate artifact attempted to modify an evaluator-protected path",
                        ),
                        protected,
                    )
                input_root = evaluation_root / ".agintor_evaluator_input"
                input_root.mkdir(parents=True, exist_ok=True)
                manifest_path = input_root / "candidate_files.json"
                manifest_path.write_text(json.dumps(normalized_files, sort_keys=True), encoding="utf-8")
                argv = (
                    *self.command_backend.python_argv,
                    "-c",
                    _DIRECT_FILE_APPLY_SCRIPT,
                    "../.agintor_evaluator_input/candidate_files.json",
                )
                return (
                    self._execute(
                        evaluation_root=evaluation_root,
                        repo_root=repo_root,
                        name="apply_candidate_files",
                        argv=argv,
                        environment=_base_execution_env(self.command_backend),
                        timeout_s=fixture.timeout_s,
                        display_command=[*self.command_backend.python_argv, "-c", "<evaluator-direct-file-apply>"],
                    ),
                    [],
                )

            patch_text = "\n".join(
                text
                for text in _patch_texts(
                    candidate_artifact,
                    repo_root,
                    allow_legacy_absolute=not self.command_backend.is_isolated,
                )
                if text.strip()
            )
            if not patch_text.strip():
                return (
                    self._failed_result(
                        name="apply_patch",
                        command=[*self.command_backend.git_argv, "apply", "<candidate.patch>"],
                        stderr="candidate artifact did not contain a patch",
                    ),
                    [],
                )
            patch_paths = _diff_target_paths(patch_text)
            if not patch_paths:
                raise ValueError("candidate patch did not declare any validated repository target paths")
            protected = _candidate_protected_paths(patch_paths, fixture.protected_paths)
            if protected:
                return (
                    self._failed_result(
                        name="apply_patch",
                        command=["<rejected-protected-patch>", patch_digest],
                        stderr="candidate patch attempted to modify an evaluator-protected path",
                    ),
                    protected,
                )
            input_root = evaluation_root / ".agintor_evaluator_input"
            input_root.mkdir(parents=True, exist_ok=True)
            patch_path = input_root / "candidate.patch"
            patch_path.write_bytes(patch_text.encode("utf-8"))
            argv = (
                *self.command_backend.git_argv,
                "apply",
                "--whitespace=nowarn",
                "--",
                "../.agintor_evaluator_input/candidate.patch",
            )
            return (
                self._execute(
                    evaluation_root=evaluation_root,
                    repo_root=repo_root,
                    name="apply_patch",
                    argv=argv,
                    environment=_base_execution_env(self.command_backend),
                    timeout_s=fixture.timeout_s,
                ),
                [],
            )
        except Exception as exc:
            return (
                self._failed_result(
                    name="apply_patch",
                    command=["<candidate-validation>", patch_digest],
                    stderr=str(exc),
                ),
                [],
            )

    def _run_command(
        self,
        evaluation_root: Path,
        repo_root: Path,
        command: RepoPatchCommand,
        fixture: RepoPatchFixture,
        *,
        sealed: bool,
    ) -> RepoPatchCommandResult:
        argv = self.command_backend.command_argv(command.command)
        return self._execute(
            evaluation_root=evaluation_root,
            repo_root=repo_root,
            name=command.name,
            argv=argv,
            environment=_effective_command_env(fixture, command, backend=self.command_backend),
            timeout_s=float(command.timeout_s or fixture.timeout_s),
            working_directory=command.working_directory,
            expected_exit_codes=command.expected_exit_codes,
            sealed=sealed,
        )

    def _execute(
        self,
        *,
        evaluation_root: Path,
        repo_root: Path | None,
        name: str,
        argv: Sequence[str],
        environment: Mapping[str, str],
        timeout_s: float,
        working_directory: str = ".",
        expected_exit_codes: Sequence[int] = (0,),
        display_command: Sequence[str] | None = None,
        sealed: bool = False,
    ) -> RepoPatchCommandResult:
        request_working_directory = "repo"
        if working_directory != ".":
            request_working_directory = f"repo/{working_directory}"
        request = IsolatedCommandRequest(
            command=tuple(str(part) for part in argv),
            workspace=evaluation_root,
            working_directory=request_working_directory,
            environment=dict(environment),
            timeout_s=timeout_s,
        )
        physical_clean_before = repo_snapshot_digest(repo_root) if repo_root is not None else ""
        try:
            if self.command_backend.is_isolated:
                prepare_container_mount_tree(evaluation_root)
            result = self.command_backend.run(request)
        except Exception as exc:
            result = IsolatedCommandResult(
                status=IsolatedCommandStatus.LAUNCH_FAILED,
                command=request.command,
                container_name="repo-patch-backend-launch-failed",
                exit_code=None,
                stdout="",
                stderr="",
                stdout_digest=stable_hash(b""),
                stderr_digest=stable_hash(b""),
                duration_s=0.0,
                failure_detail=str(exc),
            )
        physical_clean_after = repo_snapshot_digest(repo_root) if repo_root is not None else ""
        workspace_before, workspace_after, workspace_source = _authoritative_workspace_transition(
            self.command_backend,
            physical_clean_before=physical_clean_before,
            physical_clean_after=physical_clean_after,
        )
        return _command_result_from_backend(
            name=name,
            backend_id=self.command_backend.backend_id,
            request_command=request.command,
            result=result,
            display_command=list(display_command) if display_command is not None else list(request.command),
            expected_exit_codes=tuple(int(code) for code in expected_exit_codes),
            sealed=sealed,
            workspace_digest_before=workspace_before,
            workspace_digest_after=workspace_after,
            workspace_digest_source=workspace_source,
        )

    def _failed_result(self, *, name: str, command: Sequence[str], stderr: str) -> RepoPatchCommandResult:
        return _command_result(
            name=name,
            command=list(command),
            backend_id=self.command_backend.backend_id,
            terminal_status=IsolatedCommandStatus.COMPLETED.value,
            exit_code=1,
            expected_exit_codes=(0,),
            stdout="",
            stderr=stderr,
            duration_s=0.0,
        )


def _command_result(
    *,
    name: str,
    command: str | list[str],
    backend_id: str,
    terminal_status: str,
    exit_code: int,
    expected_exit_codes: Sequence[int],
    stdout: str,
    stderr: str,
    duration_s: float,
    timed_out: bool = False,
    output_truncated: bool = False,
    failure_detail: str | None = None,
    sealed: bool = False,
    digest_command: Sequence[str] | str | None = None,
    stdout_digest: str | None = None,
    stderr_digest: str | None = None,
    workspace_digest_before: str = "",
    workspace_digest_after: str = "",
    workspace_digest_source: str = "",
) -> RepoPatchCommandResult:
    effective_stdout_digest = stdout_digest or stable_hash(stdout)
    effective_stderr_digest = stderr_digest or stable_hash(stderr)
    return RepoPatchCommandResult(
        name=name,
        command=command,
        command_digest=stable_hash("repo_patch.command", name, digest_command or command),
        backend_id=backend_id,
        terminal_status=terminal_status,
        exit_code=exit_code,
        expected_exit_codes=tuple(int(code) for code in expected_exit_codes),
        stdout_digest=effective_stdout_digest,
        stderr_digest=effective_stderr_digest,
        log_digest=stable_hash(effective_stdout_digest, effective_stderr_digest),
        duration_s=duration_s,
        timed_out=timed_out,
        output_truncated=output_truncated,
        failure_detail=failure_detail,
        sealed=sealed,
        workspace_digest_before=workspace_digest_before,
        workspace_digest_after=workspace_digest_after,
        workspace_digest_source=workspace_digest_source,
        stdout="" if sealed else stdout[-4000:],
        stderr="" if sealed else stderr[-4000:],
    )


def _command_result_from_backend(
    *,
    name: str,
    backend_id: str,
    request_command: Sequence[str],
    result: IsolatedCommandResult,
    display_command: list[str],
    expected_exit_codes: tuple[int, ...],
    sealed: bool,
    workspace_digest_before: str = "",
    workspace_digest_after: str = "",
    workspace_digest_source: str = "",
) -> RepoPatchCommandResult:
    exit_code = result.exit_code
    if exit_code is None:
        exit_code = {
            IsolatedCommandStatus.TIMED_OUT: 124,
            IsolatedCommandStatus.OUTPUT_LIMIT: 125,
            IsolatedCommandStatus.LAUNCH_FAILED: 126,
        }.get(result.status, 125)
    failure_detail = result.failure_detail
    if sealed and failure_detail:
        failure_detail = f"sealed command ended with status {result.status.value}"
    return _command_result(
        name=name,
        command=["<sealed-command>"] if sealed else display_command,
        digest_command=request_command,
        backend_id=backend_id,
        terminal_status=result.status.value,
        exit_code=int(exit_code),
        expected_exit_codes=expected_exit_codes,
        stdout=result.stdout,
        stderr=result.stderr,
        stdout_digest=result.stdout_digest,
        stderr_digest=result.stderr_digest,
        duration_s=result.duration_s,
        timed_out=result.status == IsolatedCommandStatus.TIMED_OUT,
        output_truncated=result.output_truncated,
        failure_detail=failure_detail,
        sealed=sealed,
        workspace_digest_before=workspace_digest_before,
        workspace_digest_after=workspace_digest_after,
        workspace_digest_source=workspace_digest_source,
    )


def _command_succeeded(result: RepoPatchCommandResult) -> bool:
    return (
        result.terminal_status == IsolatedCommandStatus.COMPLETED.value
        and result.exit_code in result.expected_exit_codes
        and not result.timed_out
        and not result.output_truncated
    )


def _authoritative_workspace_transition(
    backend: RepoPatchExecutionBackend,
    *,
    physical_clean_before: str,
    physical_clean_after: str,
) -> tuple[str, str, str]:
    transition = _last_workspace_transition(backend)
    if transition is not None and transition.get("source") == "replay_manifest":
        return (
            str(transition["before"]),
            str(transition["after"]),
            "backend_recorded_workspace",
        )
    return physical_clean_before, physical_clean_after, "physical_clean_repo"


def _last_workspace_transition(backend: RepoPatchExecutionBackend) -> dict[str, str] | None:
    raw = getattr(backend, "last_workspace_transition", None)
    if callable(raw):
        raw = raw()
    if raw is None or not isinstance(raw, Mapping):
        return None
    before = str(raw.get("before", "") or "")
    after = str(raw.get("after", "") or "")
    source = str(raw.get("source", "") or "")
    if not before or not after or not source:
        return None
    return {"before": before, "after": after, "source": source}


def _workspace_integrity_check(
    *,
    phase: Literal["patch_apply", "public_check", "sealed_check"],
    command_result: RepoPatchCommandResult,
    command_index: int,
    expected_digest: str,
    compare_before: bool = True,
) -> RepoPatchWorkspaceIntegrityCheck:
    before = command_result.workspace_digest_before
    after = command_result.workspace_digest_after
    matched = bool(expected_digest and after == expected_digest)
    if compare_before:
        matched = matched and before == expected_digest
    return RepoPatchWorkspaceIntegrityCheck(
        phase=phase,
        command_name=command_result.name,
        command_index=command_index,
        sealed=command_result.sealed,
        expected_digest=expected_digest,
        before_digest=before,
        after_digest=after,
        digest_source=command_result.workspace_digest_source or "unavailable",
        matched=matched,
    )


def _workspace_drift_detected(
    checks: Sequence[RepoPatchWorkspaceIntegrityCheck],
) -> bool:
    return any(not check.matched for check in checks)


def _base_command_env() -> dict[str, str]:
    return {
        key: str(value)
        for key, value in os.environ.items()
        if key.upper() in _INHERITED_ENV_ALLOWLIST
    }


def _base_execution_env(backend: RepoPatchExecutionBackend) -> dict[str, str]:
    if backend.is_isolated:
        return {
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
    return _base_command_env()


def _effective_command_env(
    fixture: RepoPatchFixture,
    command: RepoPatchCommand,
    *,
    backend: RepoPatchExecutionBackend,
) -> dict[str, str]:
    env = _base_execution_env(backend)
    env.update({str(key): str(value) for key, value in fixture.command_env.items()})
    env.update({str(key): str(value) for key, value in command.env.items()})
    return dict(sorted(env.items()))


def _safe_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {
            str(key): _safe_payload(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
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


def _patch_texts(
    candidate_artifact: Any,
    repo_root: Path,
    *,
    allow_legacy_absolute: bool,
) -> list[str]:
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
                texts.append(
                    _normalize_diff_paths(
                        str(row["diff"]),
                        _row_relative_path(row, repo_root, allow_legacy_absolute=allow_legacy_absolute),
                    )
                )
    return texts


def _direct_file_updates(
    candidate_artifact: Any,
    repo_root: Path,
    *,
    allow_legacy_absolute: bool,
) -> list[tuple[str, str]]:
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
            rel_path = _row_relative_path(row, repo_root, allow_legacy_absolute=allow_legacy_absolute)
            if not rel_path or "updated_content" not in row:
                continue
            updates.append((rel_path, str(row.get("updated_content", ""))))
    return updates


def _row_relative_path(
    row: Mapping[str, Any],
    repo_root: Path,
    *,
    allow_legacy_absolute: bool,
) -> str:
    for key in ("relative_path", "repo_relative_path", "path"):
        rel_path = _repo_relative_path(
            row.get(key),
            repo_root,
            allow_legacy_absolute=allow_legacy_absolute,
        )
        if rel_path:
            return rel_path
    return ""


def _repo_relative_path(value: Any, repo_root: Path, *, allow_legacy_absolute: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("\\", "/")
    if not _looks_absolute_path(normalized):
        return normalized
    if not allow_legacy_absolute:
        return normalized
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
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path))


def _validated_repo_relative_path(value: Any, *, label: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ValueError(f"{label} must be a non-empty NUL-free repository-relative path")
    path = PurePosixPath(raw)
    if _looks_absolute_path(raw) or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must stay within the evaluator scratch repository: {value!r}")
    return path.as_posix()


def _candidate_protected_paths(
    candidate_paths: Sequence[str],
    protected_paths: Sequence[str],
) -> list[str]:
    protected = [*protected_paths, ".git"]
    changed: list[str] = []
    for candidate_path in candidate_paths:
        normalized = _validated_repo_relative_path(candidate_path, label="candidate patch path")
        for protected_path in protected:
            normalized_protected = _validated_repo_relative_path(protected_path, label="protected path")
            if normalized == normalized_protected or normalized.startswith(f"{normalized_protected}/"):
                changed.append(normalized_protected)
    return sorted(set(changed))


def _diff_target_paths(diff_text: str) -> list[str]:
    paths: list[str] = []
    in_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            fields = shlex.split(line)
            if len(fields) != 4:
                raise ValueError("malformed diff --git header")
            paths.extend([_strip_diff_prefix(fields[2]), _strip_diff_prefix(fields[3])])
            continue
        if line.startswith("--- "):
            in_header = True
            path = _diff_header_path(line[4:])
            if path != "/dev/null":
                paths.append(_strip_diff_prefix(path))
            continue
        if in_header and line.startswith("+++ "):
            path = _diff_header_path(line[4:])
            if path != "/dev/null":
                paths.append(_strip_diff_prefix(path))
            continue
        if line.startswith("@@") or line == "GIT binary patch":
            in_header = False
    return sorted({_validated_repo_relative_path(path, label="candidate patch path") for path in paths})


def validate_unified_diff_paths(
    diff_text: str,
    *,
    protected_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Validate one unified diff without applying it or touching a backend.

    This is the public preflight companion to the evaluator runner's patch
    application path.  It deliberately reuses the exact path parser and
    protected-path policy used during isolated execution so dry-run manifests
    cannot describe a patch that E1 would interpret differently.
    """

    if not isinstance(diff_text, str) or not diff_text.strip():
        raise ValueError("candidate patch must be a nonempty unified diff")
    if "\x00" in diff_text:
        raise ValueError("candidate patch must be NUL-free")
    paths = _diff_target_paths(diff_text)
    if not paths:
        raise ValueError("candidate patch did not declare any validated repository target paths")
    protected = _candidate_protected_paths(paths, protected_paths)
    if protected:
        raise ValueError("candidate patch attempted to modify an evaluator-protected path")
    return tuple(paths)


def _diff_header_path(raw_header: str) -> str:
    header = raw_header.split("\t", 1)[0].strip()
    if not header:
        raise ValueError("empty unified-diff path header")
    if header.startswith('"'):
        fields = shlex.split(header)
        if not fields:
            raise ValueError("malformed quoted unified-diff path header")
        return fields[0]
    return header


def _strip_diff_prefix(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        return normalized[2:]
    return normalized


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

__all__ = [
    "IsolatedRepoPatchCommandBackend",
    "RepoPatchCommand",
    "RepoPatchCommandResult",
    "RepoPatchEvaluatorRunner",
    "RepoPatchFixture",
    "RepoPatchRunnerResult",
    "RepoPatchWorkspaceIntegrityCheck",
    "TrustedLocalRepoPatchCommandBackend",
    "command_suite_digest",
    "environment_digest",
    "protected_path_digest",
    "repo_patch_fixture_digest",
    "repo_snapshot_digest",
    "validate_unified_diff_paths",
]
