from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...contracts.epochs import TaskEnvelope, TrustedToolId
from ...core.identity import canonical_identity_digest
from ...isolation.commands import (
    IsolatedCommandBackend,
    IsolatedCommandRequest,
    IsolatedCommandResult,
    IsolatedCommandStatus,
)
from ...isolation.workspaces import (
    ContainerMountWorkspaceError,
    prepare_container_mount_tree,
    private_container_mount_workspace,
)
from ...repositories.workspaces import (
    RepositorySnapshotError,
    TaskWorkspace,
    copy_repository_snapshot,
    repository_snapshot_digest,
    unified_diff_between,
)
from .composite_budget import (
    AggregateBudgetLedger,
    AggregateBudgetSnapshot,
    BudgetExhaustedError,
)


FIXED_REPAIR_TOOL_IDS = (
    "repo.search",
    "repo.read",
    "repo.public_test",
    "repo.edit",
    "repo.diff",
)
FIXED_PROTECTED_PATHS = (".agintor", ".git", "tests")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")


class RepairToolStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"
    LAUNCH_FAILED = "launch_failed"
    BUDGET_REJECTED = "budget_rejected"


class RepairToolLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_read_bytes: int = Field(default=64_000, gt=0)
    max_read_lines: int = Field(default=2_000, gt=0)
    max_search_files: int = Field(default=5_000, gt=0)
    max_search_results: int = Field(default=200, gt=0)
    max_search_output_bytes: int = Field(default=64_000, gt=0)
    max_edit_bytes: int = Field(default=256_000, gt=0)
    max_command_output_bytes: int = Field(default=128_000, gt=0)
    max_receipt_bytes: int = Field(default=256_000, gt=0)


class IsolatedCommandEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    command: tuple[str, ...]
    working_directory: str
    timeout_s: float = Field(gt=0.0)
    status: str
    exit_code: int | None = None
    stdout: str
    stderr: str
    stdout_digest: str
    stderr_digest: str
    duration_s: float = Field(ge=0.0)
    output_truncated: bool
    passed: bool


class RepairToolReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str
    call_id: str
    tool_id: str
    phase: Literal["actor_tool", "terminal_public_verification"]
    tool_request_id: str | None = None
    verification_step_id: str | None = None
    status: RepairToolStatus
    arguments_digest: str
    output: Any
    output_digest: str
    output_bytes: int = Field(ge=0)
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)
    error_code: str | None = None
    workspace_digest_before: str
    workspace_digest_after: str
    immutable_base_unchanged: bool
    source_snapshot_unchanged: bool
    command_evidence: tuple[IsolatedCommandEvidence, ...] = ()
    charged: bool
    ledger_after: AggregateBudgetSnapshot

    @model_validator(mode="after")
    def validate_origin(self) -> "RepairToolReceipt":
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("repair-tool receipt finish precedes start")
        if self.phase == "actor_tool":
            if not self.tool_request_id or self.verification_step_id is not None:
                raise ValueError("actor-tool receipts require only a tool_request_id")
        elif (
            self.tool_id != "repo.public_test"
            or self.tool_request_id is not None
            or not self.verification_step_id
        ):
            raise ValueError(
                "terminal public-verification receipts require only a verification_step_id"
            )
        return self


class RepairToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: RepairToolReceipt

    @property
    def succeeded(self) -> bool:
        return self.receipt.status is RepairToolStatus.SUCCEEDED

    @property
    def output(self) -> Any:
        return self.receipt.output


class PublicVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    receipt_ids: tuple[str, ...]
    command_evidence: tuple[IsolatedCommandEvidence, ...]


class RepairToolError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TrustedRepairToolService:
    """The fixed repo-repair-v1 tool authority over one materialized workspace."""

    def __init__(
        self,
        task: TaskEnvelope,
        workspace: TaskWorkspace,
        command_backend: IsolatedCommandBackend,
        *,
        limits: RepairToolLimits | None = None,
    ) -> None:
        self.task = TaskEnvelope.model_validate(task.model_dump(mode="python"))
        self.workspace = TaskWorkspace.model_validate(workspace.model_dump(mode="python"))
        self.command_backend = command_backend
        self.limits = limits or RepairToolLimits()
        if self.workspace.snapshot_id != self.task.workspace_snapshot.snapshot_id:
            raise ValueError("tool workspace snapshot id does not match TaskEnvelope")
        if self.workspace.snapshot_digest != self.task.workspace_snapshot.digest:
            raise ValueError("tool workspace digest does not match TaskEnvelope")
        self._lock = threading.RLock()
        self._sequence = 0
        self._receipts: list[RepairToolReceipt] = []
        self._assert_workspace_roots()
        if not self.immutable_source_unchanged():
            raise ValueError("tool workspace immutable roots do not match their snapshot")
        if self.current_workspace_digest() != self.workspace.snapshot_digest:
            raise ValueError("tool working copy is not initially clean")
        self._protected_tree_baseline = self._protected_tree_digest()

    def _assert_workspace_roots(self) -> None:
        roots = (
            self.workspace.source_root.resolve(),
            self.workspace.immutable_base_root.resolve(),
            self.workspace.working_root.resolve(),
        )
        if len(set(roots)) != 3:
            raise ValueError("source, immutable base, and working roots must be distinct")
        if not all(root.is_dir() for root in roots):
            raise ValueError("all task workspace roots must exist")

    def current_workspace_digest(self) -> str:
        return repository_snapshot_digest(self.workspace.working_root)

    def immutable_source_unchanged(self) -> bool:
        return self.source_snapshot_unchanged() and self.immutable_base_unchanged()

    def source_snapshot_unchanged(self) -> bool:
        try:
            return repository_snapshot_digest(self.workspace.source_root) == self.workspace.snapshot_digest
        except RepositorySnapshotError:
            return False

    def immutable_base_unchanged(self) -> bool:
        try:
            return repository_snapshot_digest(self.workspace.immutable_base_root) == self.workspace.snapshot_digest
        except RepositorySnapshotError:
            return False

    def workspace_diff(self, *, max_patch_bytes: int) -> str:
        if not self.immutable_source_unchanged():
            raise RepairToolError("immutable_source_changed")
        if not self.protected_tree_unchanged():
            raise RepairToolError("protected_path_changed")
        return unified_diff_between(
            self.workspace.immutable_base_root,
            self.workspace.working_root,
            max_patch_bytes=max_patch_bytes,
        )

    def _protected_tree_digest(self) -> str:
        records: list[dict[str, Any]] = []
        root = self.workspace.working_root
        for protected in FIXED_PROTECTED_PATHS:
            protected_root = root / protected
            if protected_root.is_symlink():
                records.append(
                    {
                        "path": protected,
                        "kind": "symlink",
                        "target": os.readlink(protected_root),
                    }
                )
                continue
            if not protected_root.exists():
                records.append({"path": protected, "kind": "missing"})
                continue
            if protected_root.is_file():
                records.append(
                    {
                        "path": protected,
                        "kind": "file",
                        "sha256": hashlib.sha256(protected_root.read_bytes()).hexdigest(),
                    }
                )
                continue
            records.append({"path": protected, "kind": "directory"})
            for path in sorted(protected_root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    records.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "target": os.readlink(path),
                        }
                    )
                elif path.is_file():
                    records.append(
                        {
                            "path": relative,
                            "kind": "file",
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
                elif path.is_dir():
                    records.append({"path": relative, "kind": "directory"})
                else:
                    records.append({"path": relative, "kind": "special"})
        return canonical_identity_digest(records, domain="repair-protected-tree")

    def protected_tree_unchanged(self) -> bool:
        try:
            return self._protected_tree_digest() == self._protected_tree_baseline
        except OSError:
            return False

    @staticmethod
    def _workspace_digest_matches(root: Path, expected_digest: str) -> bool:
        try:
            return repository_snapshot_digest(root) == expected_digest
        except RepositorySnapshotError:
            return False

    def receipts(self) -> tuple[RepairToolReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def _next_receipt_id(self) -> str:
        self._sequence += 1
        return f"tool.receipt.{self._sequence:06d}"

    def _output_limit(self, tool_id: str) -> int:
        per_tool = {
            "repo.search": self.limits.max_search_output_bytes + 4096,
            "repo.read": self.limits.max_read_bytes + 4096,
            "repo.public_test": self.limits.max_command_output_bytes + 4096,
            "repo.edit": 4096,
            "repo.diff": self.task.ceilings.max_patch_bytes + 4096,
        }.get(tool_id, 4096)
        return min(per_tool, self.task.ceilings.max_tool_output_bytes)

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")

    def invoke(
        self,
        *,
        call_id: str,
        tool_id: TrustedToolId,
        arguments: Mapping[str, Any],
        ledger: AggregateBudgetLedger,
        phase: Literal["actor_tool", "terminal_public_verification"],
        tool_request_id: str | None,
        verification_step_id: str | None,
    ) -> RepairToolResult:
        with self._lock:
            started_at_ms = time.time_ns() // 1_000_000
            normalized_tool_id = str(tool_id)
            output_limit = max(
                1,
                min(
                    self._output_limit(normalized_tool_id),
                    ledger.remaining_tool_output_bytes(),
                ),
            )
            before = self.current_workspace_digest()
            try:
                reservation = ledger.reserve_tool_call(max_output_bytes=output_limit)
            except BudgetExhaustedError as exc:
                output = {"error": "tool_budget_exhausted", "metric": exc.metric}
                snapshot = ledger.snapshot()
                receipt = RepairToolReceipt(
                    receipt_id=self._next_receipt_id(),
                    call_id=call_id,
                    tool_id=normalized_tool_id,
                    phase=phase,
                    tool_request_id=tool_request_id,
                    verification_step_id=verification_step_id,
                    status=RepairToolStatus.BUDGET_REJECTED,
                    arguments_digest=canonical_identity_digest(
                        dict(arguments), domain="repair-tool-arguments"
                    ),
                    output=output,
                    output_digest=canonical_identity_digest(
                        output, domain="repair-tool-output"
                    ),
                    output_bytes=len(self._json_bytes(output)),
                    started_at_ms=started_at_ms,
                    finished_at_ms=time.time_ns() // 1_000_000,
                    error_code="tool_budget_exhausted",
                    workspace_digest_before=before,
                    workspace_digest_after=self.current_workspace_digest(),
                    immutable_base_unchanged=self.immutable_base_unchanged(),
                    source_snapshot_unchanged=self.source_snapshot_unchanged(),
                    charged=False,
                    ledger_after=snapshot,
                )
                self._receipts.append(receipt)
                return RepairToolResult(receipt=receipt)

            status = RepairToolStatus.SUCCEEDED
            error_code: str | None = None
            command_evidence: tuple[IsolatedCommandEvidence, ...] = ()
            try:
                if normalized_tool_id not in FIXED_REPAIR_TOOL_IDS:
                    raise RepairToolError("unsupported_tool")
                if not self.immutable_source_unchanged():
                    raise RepairToolError("immutable_source_changed")
                output, command_evidence = self._execute(
                    normalized_tool_id,
                    dict(arguments),
                )
                if normalized_tool_id == "repo.public_test":
                    public_error = (
                        output.get("error")
                        if isinstance(output, dict) and isinstance(output.get("error"), str)
                        else None
                    )
                    if public_error == "public_test_workspace_changed":
                        status = RepairToolStatus.FAILED
                        error_code = public_error
                    elif not all(item.passed for item in command_evidence):
                        status = self._public_failure_status(command_evidence)
                        error_code = "public_reproduction_failed"
            except RepairToolError as exc:
                status = RepairToolStatus.FAILED
                error_code = exc.code
                output = {"error": exc.code}
            except RepositorySnapshotError:
                status = RepairToolStatus.FAILED
                error_code = "repository_integrity_error"
                output = {"error": error_code}
            except Exception as exc:
                status = RepairToolStatus.FAILED
                error_code = f"internal_{type(exc).__name__}"
                output = {"error": "repair_tool_internal_failure"}

            immutable_unchanged = self.immutable_source_unchanged()
            if not immutable_unchanged:
                status = RepairToolStatus.FAILED
                error_code = "immutable_source_changed"
                output = {"error": error_code}
            encoded = self._json_bytes(output)
            if len(encoded) > output_limit or len(encoded) > self.limits.max_receipt_bytes:
                status = RepairToolStatus.OUTPUT_LIMIT
                error_code = "tool_output_limit"
                output = {"error": error_code}
                encoded = self._json_bytes(output)
            after = self.current_workspace_digest()
            finished_at_ms = time.time_ns() // 1_000_000
            snapshot = ledger.complete_tool_call(
                reservation,
                output_bytes=len(encoded),
            )
            receipt = RepairToolReceipt(
                receipt_id=self._next_receipt_id(),
                call_id=call_id,
                tool_id=normalized_tool_id,
                phase=phase,
                tool_request_id=tool_request_id,
                verification_step_id=verification_step_id,
                status=status,
                arguments_digest=canonical_identity_digest(
                    dict(arguments), domain="repair-tool-arguments"
                ),
                output=output,
                output_digest=canonical_identity_digest(
                    output, domain="repair-tool-output"
                ),
                output_bytes=len(encoded),
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
                error_code=error_code,
                workspace_digest_before=before,
                workspace_digest_after=after,
                immutable_base_unchanged=self.immutable_base_unchanged(),
                source_snapshot_unchanged=self.source_snapshot_unchanged(),
                command_evidence=command_evidence,
                charged=True,
                ledger_after=snapshot,
            )
            self._receipts.append(receipt)
            return RepairToolResult(receipt=receipt)

    @staticmethod
    def _public_failure_status(
        evidence: tuple[IsolatedCommandEvidence, ...],
    ) -> RepairToolStatus:
        if any(item.status == IsolatedCommandStatus.TIMED_OUT.value for item in evidence):
            return RepairToolStatus.TIMED_OUT
        if any(item.status == IsolatedCommandStatus.OUTPUT_LIMIT.value for item in evidence):
            return RepairToolStatus.OUTPUT_LIMIT
        if any(item.status == IsolatedCommandStatus.LAUNCH_FAILED.value for item in evidence):
            return RepairToolStatus.LAUNCH_FAILED
        return RepairToolStatus.FAILED

    def _execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
    ) -> tuple[Any, tuple[IsolatedCommandEvidence, ...]]:
        if tool_id == "repo.search":
            return self._search(arguments), ()
        if tool_id == "repo.read":
            return self._read(arguments), ()
        if tool_id == "repo.edit":
            return self._edit(arguments), ()
        if tool_id == "repo.diff":
            self._require_exact_keys(arguments, set())
            return {
                "patch": self.workspace_diff(
                    max_patch_bytes=self.task.ceilings.max_patch_bytes
                )
            }, ()
        if tool_id == "repo.public_test":
            return self._public_test(arguments)
        raise RepairToolError("unsupported_tool")

    @staticmethod
    def _require_exact_keys(arguments: Mapping[str, Any], allowed: set[str]) -> None:
        if set(arguments) != allowed:
            raise RepairToolError("invalid_arguments")

    def _relative_path(
        self,
        raw: Any,
        *,
        allow_root: bool,
    ) -> tuple[str, Path]:
        text = str(raw or ".").strip().replace("\\", "/") or "."
        if "\x00" in text or _WINDOWS_ABSOLUTE_RE.match(text):
            raise RepairToolError("invalid_path")
        pure = PurePosixPath(text)
        if pure.is_absolute() or ".." in pure.parts:
            raise RepairToolError("invalid_path")
        normalized = pure.as_posix()
        if normalized == "." and not allow_root:
            raise RepairToolError("invalid_path")
        candidate = (self.workspace.working_root / normalized).resolve()
        working = self.workspace.working_root.resolve()
        if candidate != working and working not in candidate.parents:
            raise RepairToolError("path_escape")
        return normalized, candidate

    @staticmethod
    def _is_protected(relative: str) -> bool:
        return any(
            relative == protected or relative.startswith(f"{protected}/")
            for protected in FIXED_PROTECTED_PATHS
        )

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"query", "path", "max_results", "case_sensitive"}
        if not set(arguments) <= allowed or "query" not in arguments:
            raise RepairToolError("invalid_arguments")
        query = str(arguments["query"])
        if not query or "\x00" in query or len(query.encode("utf-8")) > 4096:
            raise RepairToolError("invalid_query")
        relative, root = self._relative_path(arguments.get("path", "."), allow_root=True)
        if not root.exists() or not root.is_dir():
            raise RepairToolError("path_not_directory")
        try:
            max_results = int(arguments.get("max_results", self.limits.max_search_results))
        except (TypeError, ValueError) as exc:
            raise RepairToolError("invalid_arguments") from exc
        if max_results <= 0 or max_results > self.limits.max_search_results:
            raise RepairToolError("invalid_arguments")
        case_sensitive = arguments.get("case_sensitive", True)
        if not isinstance(case_sensitive, bool):
            raise RepairToolError("invalid_arguments")
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        files_seen = 0
        output_bytes = 0
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise RepairToolError("symlink_forbidden")
            if not path.is_file():
                continue
            files_seen += 1
            if files_seen > self.limits.max_search_files:
                raise RepairToolError("search_file_limit")
            if path.stat().st_size > self.limits.max_read_bytes:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                item = {
                    "path": path.relative_to(self.workspace.working_root).as_posix(),
                    "line": line_number,
                    "text": line,
                }
                item_bytes = len(self._json_bytes(item))
                if output_bytes + item_bytes > self.limits.max_search_output_bytes:
                    return {"matches": matches, "truncated": True, "root": relative}
                matches.append(item)
                output_bytes += item_bytes
                if len(matches) >= max_results:
                    return {"matches": matches, "truncated": True, "root": relative}
        return {"matches": matches, "truncated": False, "root": relative}

    def _read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"path", "start_line", "max_lines"}
        if not set(arguments) <= allowed or "path" not in arguments:
            raise RepairToolError("invalid_arguments")
        relative, path = self._relative_path(arguments["path"], allow_root=False)
        if not path.is_file() or path.is_symlink():
            raise RepairToolError("path_not_file")
        if path.stat().st_size > self.limits.max_read_bytes:
            raise RepairToolError("read_byte_limit")
        try:
            start_line = int(arguments.get("start_line", 1))
            max_lines = int(arguments.get("max_lines", self.limits.max_read_lines))
        except (TypeError, ValueError) as exc:
            raise RepairToolError("invalid_arguments") from exc
        if start_line <= 0 or max_lines <= 0 or max_lines > self.limits.max_read_lines:
            raise RepairToolError("invalid_arguments")
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise RepairToolError("non_utf8_file") from exc
        selected = "".join(lines[start_line - 1 : start_line - 1 + max_lines])
        if len(selected.encode("utf-8")) > self.limits.max_read_bytes:
            raise RepairToolError("read_byte_limit")
        return {
            "path": relative,
            "start_line": start_line,
            "content": selected,
            "truncated": start_line - 1 + max_lines < len(lines),
        }

    def _edit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_exact_keys(arguments, {"path", "content"})
        relative, path = self._relative_path(arguments["path"], allow_root=False)
        if self._is_protected(relative):
            raise RepairToolError("protected_path")
        content = arguments["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise RepairToolError("invalid_content")
        encoded = content.encode("utf-8")
        if len(encoded) > self.limits.max_edit_bytes:
            raise RepairToolError("edit_byte_limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = path.parent.resolve()
        working = self.workspace.working_root.resolve()
        if resolved_parent != working and working not in resolved_parent.parents:
            raise RepairToolError("path_escape")
        temp = path.parent / f".agintor-edit-{os.getpid()}-{self._sequence + 1}"
        try:
            temp.write_text(content, encoding="utf-8", newline="")
            temp.replace(path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            "path": relative,
            "bytes_written": len(encoded),
            "content_digest": canonical_identity_digest(
                content, domain="repair-edit-content"
            ),
        }

    def _public_test(
        self,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[IsolatedCommandEvidence, ...]]:
        if set(arguments) not in (set(), {"step_id"}):
            raise RepairToolError("invalid_arguments")
        requested_step = str(arguments.get("step_id", "")).strip()
        steps = tuple(
            step
            for step in self.task.public_reproduction
            if not requested_step or step.step_id == requested_step
        )
        if not steps:
            raise RepairToolError("unknown_public_step")
        evidence: list[IsolatedCommandEvidence] = []
        workspace_digest_before = self.current_workspace_digest()
        if not self.protected_tree_unchanged():
            raise RepairToolError("protected_path_changed")
        try:
            with private_container_mount_workspace(
                prefix=".agintor-public-test-",
                parent=self.workspace.working_root.resolve().parent,
            ) as public_workspace:
                copy_repository_snapshot(self.workspace.working_root, public_workspace)
                if not self._workspace_digest_matches(
                    public_workspace,
                    workspace_digest_before,
                ):
                    raise RepairToolError("public_test_workspace_copy_mismatch")
                for step in steps:
                    if not self.protected_tree_unchanged():
                        raise RepairToolError("protected_path_changed")
                    public_workspace_digest_before = repository_snapshot_digest(
                        public_workspace
                    )
                    prepare_container_mount_tree(public_workspace)
                    request = IsolatedCommandRequest(
                        command=step.argv,
                        workspace=public_workspace,
                        working_directory=step.cwd,
                        environment={},
                        timeout_s=step.timeout_ms / 1000.0,
                    )
                    try:
                        result = self.command_backend.run(request)
                    finally:
                        if not self.protected_tree_unchanged():
                            raise RepairToolError("protected_path_changed")
                        if not self._workspace_digest_matches(
                            self.workspace.working_root,
                            workspace_digest_before,
                        ):
                            raise RepairToolError("public_test_workspace_changed")
                    command_evidence = self._command_evidence(
                        step.step_id,
                        step.expected_exit_codes,
                        request,
                        result,
                    )
                    if not self._workspace_digest_matches(
                        public_workspace,
                        public_workspace_digest_before,
                    ):
                        command_evidence = command_evidence.model_copy(
                            update={"passed": False}
                        )
                        evidence.append(command_evidence)
                        rows = tuple(evidence)
                        return {
                            "passed": False,
                            "error": "public_test_workspace_changed",
                            "steps": [item.model_dump(mode="json") for item in rows],
                        }, rows
                    evidence.append(command_evidence)
        except ContainerMountWorkspaceError as exc:
            raise RepairToolError("public_test_workspace_boundary_failed") from exc
        finally:
            if not self._workspace_digest_matches(
                self.workspace.working_root,
                workspace_digest_before,
            ):
                raise RepairToolError("public_test_workspace_changed")
            if not self.protected_tree_unchanged():
                raise RepairToolError("protected_path_changed")
        rows = tuple(evidence)
        return {
            "passed": all(item.passed for item in rows),
            "steps": [item.model_dump(mode="json") for item in rows],
        }, rows

    def _command_evidence(
        self,
        step_id: str,
        expected_exit_codes: tuple[int, ...],
        request: IsolatedCommandRequest,
        result: IsolatedCommandResult,
    ) -> IsolatedCommandEvidence:
        stdout = self._bounded_text(result.stdout, self.limits.max_command_output_bytes)
        stderr = self._bounded_text(result.stderr, self.limits.max_command_output_bytes)
        passed = (
            result.status is IsolatedCommandStatus.COMPLETED
            and result.exit_code in expected_exit_codes
        )
        return IsolatedCommandEvidence(
            step_id=step_id,
            command=request.command,
            working_directory=request.working_directory,
            timeout_s=float(request.timeout_s),
            status=result.status.value,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_digest=result.stdout_digest,
            stderr_digest=result.stderr_digest,
            duration_s=result.duration_s,
            output_truncated=result.output_truncated,
            passed=passed,
        )

    @staticmethod
    def _bounded_text(value: str, max_bytes: int) -> str:
        raw = value.encode("utf-8")
        if len(raw) <= max_bytes:
            return value
        return raw[:max_bytes].decode("utf-8", errors="ignore")

    def run_public_verification(
        self,
        *,
        call_id: str,
        ledger: AggregateBudgetLedger,
    ) -> PublicVerificationResult:
        results = tuple(
            self.invoke(
                call_id=call_id,
                tool_id="repo.public_test",
                arguments={"step_id": step.step_id},
                ledger=ledger,
                phase="terminal_public_verification",
                tool_request_id=None,
                verification_step_id=step.step_id,
            )
            for step in self.task.public_reproduction
        )
        return PublicVerificationResult(
            passed=all(
                result.succeeded and bool(result.output.get("passed"))
                for result in results
            ),
            receipt_ids=tuple(result.receipt.receipt_id for result in results),
            command_evidence=tuple(
                evidence
                for result in results
                for evidence in result.receipt.command_evidence
            ),
        )


__all__ = [
    "FIXED_PROTECTED_PATHS",
    "FIXED_REPAIR_TOOL_IDS",
    "IsolatedCommandEvidence",
    "PublicVerificationResult",
    "RepairToolLimits",
    "RepairToolReceipt",
    "RepairToolResult",
    "RepairToolStatus",
    "TrustedRepairToolService",
]
