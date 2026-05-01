from __future__ import annotations

import difflib
import errno
import json
import os
import tempfile
from urllib import error as urllib_error
from urllib import request as urllib_request
from pathlib import Path
from typing import Any, Mapping, Sequence
from ....core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ...api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ....contracts import (
    AgentTemplate,
    AsyncHandle,
    BenchmarkTask,
    BranchBudget,
    BranchPlan,
    BranchPublication,
    BranchResumeSnapshot,
    BranchResult,
    BranchState,
    CancellationRecord,
    Checkpoint,
    CheckpointEnvelope,
    ChildSpec,
    ExecutionPlan,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    RecoveryFailureKind,
    ReceiptReconciliationRecord,
    ReplayAllocation,
    RunResult,
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ....utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class BoundedPathMixin:
    @classmethod
    def _collect_file_snapshots(cls, value: Any) -> list[dict[str, Any]]:
        collected: dict[str, dict[str, Any]] = {}

        def visit(candidate: Any) -> None:
            if isinstance(candidate, Mapping):
                if "path" in candidate and "content" in candidate:
                    path = str(candidate.get("path") or "").strip()
                    if path:
                        collected[path] = {
                            "path": path,
                            "content": str(candidate.get("content", "")),
                            "exists": bool(candidate.get("exists", True)),
                        }
                    return
                for nested in candidate.values():
                    visit(nested)
                return
            if isinstance(candidate, list):
                for nested in candidate:
                    visit(nested)

        visit(value)
        return [collected[path] for path in sorted(collected)]

    @staticmethod
    def _filesystem_is_read_only(policy: str) -> bool:
        normalized = str(policy or "").strip().lower()
        return "read-only" in normalized or normalized in {"readonly", "read_only", "none"}

    @staticmethod
    def _service_action_allowed(policy: str) -> bool:
        normalized = str(policy or "").strip().lower()
        return normalized not in {"", "none", "restricted", "provider-only"}

    @staticmethod
    def _path_identity(path: Path) -> str:
        resolved = path.resolve()
        rendered = str(resolved)
        return rendered.casefold() if os.name == "nt" else rendered

    @staticmethod
    def _path_within_root(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _runtime_workspace_root(self, context: PolicyContext) -> Path:
        isolation_policy = getattr(self.runtime.deployment_contract, "runtime_isolation_policy", None)
        declared_root = str(getattr(isolation_policy, "workspace_root", "") or ".").strip() or "."
        workspace_root = Path(declared_root)
        if not workspace_root.is_absolute():
            workspace_root = (context.shell.workspace / workspace_root).resolve()
        else:
            workspace_root = workspace_root.resolve()
        return workspace_root

    def _resolve_bounded_path(
        self,
        raw_path: str,
        *,
        workspace_root: Path,
        operation_kind: str,
    ) -> Path:
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            raise HardInvalidation(f"{operation_kind} path may not be empty")
        candidate = Path(cleaned).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        resolved = (workspace_root / candidate).resolve()
        if not self._path_within_root(resolved, workspace_root):
            raise HardInvalidation(
                f"{operation_kind} path {cleaned!r} escapes the runtime workspace root {workspace_root}"
            )
        return resolved
