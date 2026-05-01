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

class RepoPatchIOMixin:
    @staticmethod
    def _parse_json_provider_payload(text: str, *, operation_kind: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except Exception as exc:
            raise HardInvalidation(f"{operation_kind} provider response must be valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise HardInvalidation(f"{operation_kind} provider response must be a JSON object")
        return dict(payload)

    def _normalized_repo_patch_targets(
        self,
        context: PolicyContext,
        *,
        target_file_paths: Sequence[str],
        file_snapshots: Sequence[Mapping[str, Any]],
    ) -> tuple[Path, dict[str, Path], dict[str, Mapping[str, Any]]]:
        workspace_root = self._runtime_workspace_root(context)
        snapshot_by_identity: dict[str, Mapping[str, Any]] = {}
        for snapshot in file_snapshots:
            snapshot_path = str(snapshot.get("path", "") or "").strip()
            if not snapshot_path:
                continue
            resolved_snapshot_path = self._resolve_bounded_path(
                snapshot_path,
                workspace_root=workspace_root,
                operation_kind="repo_patch snapshot",
            )
            snapshot_by_identity[self._path_identity(resolved_snapshot_path)] = snapshot
        resolved_targets: dict[str, Path] = {}
        for raw_target_path in target_file_paths:
            resolved_target_path = self._resolve_bounded_path(
                raw_target_path,
                workspace_root=workspace_root,
                operation_kind="repo_patch target",
            )
            target_identity = self._path_identity(resolved_target_path)
            if (
                not self._path_within_root(resolved_target_path, workspace_root)
                and target_identity not in snapshot_by_identity
            ):
                raise HardInvalidation(
                    "repo_patch targets must stay inside the runtime workspace or match explicitly hydrated request files"
                )
            resolved_targets[target_identity] = resolved_target_path
        if not resolved_targets:
            raise HardInvalidation("repo_patch execution requires at least one bounded target path")
        return workspace_root, resolved_targets, snapshot_by_identity

    @classmethod
    def _normalize_repo_patch_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        target_file_paths: Sequence[str],
    ) -> dict[str, Any]:
        allowed_paths = {str(path).strip() for path in target_file_paths if str(path).strip()}
        files_payload = payload.get("files", [])
        if not isinstance(files_payload, list):
            raise HardInvalidation("repo_patch response must include a files array")
        normalized_files: list[dict[str, Any]] = []
        for raw_file in files_payload:
            if not isinstance(raw_file, Mapping):
                raise HardInvalidation("repo_patch files entries must be JSON objects")
            path = str(raw_file.get("path") or "").strip()
            if not path:
                raise HardInvalidation("repo_patch files entries must include path")
            if allowed_paths and path not in allowed_paths:
                raise HardInvalidation(f"repo_patch attempted to modify undeclared path {path!r}")
            if "updated_content" not in raw_file:
                raise HardInvalidation(f"repo_patch response for {path!r} must include updated_content")
            normalized_files.append(
                {
                    "path": path,
                    "updated_content": str(raw_file.get("updated_content", "")),
                }
            )
        return {
            "summary": str(payload.get("summary", "") or "").strip(),
            "files": normalized_files,
        }

    @staticmethod
    def _unified_diff(path: str, before: str, after: str) -> str:
        return "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=path,
                tofile=path,
                lineterm="",
            )
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            directory_fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

    @classmethod
    def _write_text_atomic(cls, path: Path, text: str) -> None:
        ensure_directory(path.parent)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        try:
            temp_path.replace(path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                try:
                    temp_path.unlink()
                finally:
                    raise
            with path.open("w", encoding="utf-8") as target:
                target.write(text)
                target.flush()
                os.fsync(target.fileno())
            temp_path.unlink(missing_ok=True)
        cls._fsync_directory(path.parent)

    def _execute_repo_patch_node(
        self,
        context: PolicyContext,
        operation: PlanNode,
        resolved_args: Mapping[str, Any],
        model_class: str,
        trace_context: OpenAITraceContext | None,
    ) -> Any:
        target_file_paths = [
            str(path).strip()
            for path in resolved_args.get("target_file_paths", context.task.file_paths)
            if str(path).strip()
        ]
        file_snapshots = self._collect_file_snapshots(resolved_args)
        if not target_file_paths or not file_snapshots:
            raise HardInvalidation("repo_patch execution requires explicit target files and readable file snapshots")
        workspace_root, resolved_targets, snapshot_by_identity = self._normalized_repo_patch_targets(
            context,
            target_file_paths=target_file_paths,
            file_snapshots=file_snapshots,
        )
        response = context.run_model_request(
            instructions=(
                "Return JSON only with keys summary and files. "
                "files must be an array of {path, updated_content}. "
                "Modify only the provided target files."
            ),
            prompt="\n".join(
                [
                    *self._prompt_lines_with_session_carryover(context),
                    "Target files:",
                    json.dumps(file_snapshots, sort_keys=True, default=str),
                ]
            ),
            model_class=model_class,
            purpose="repo_patch",
            payload={
                "prompt": context.task.prompt,
                "target_file_paths": target_file_paths,
            },
            trace_context=trace_context,
        )
        patch_payload = self._normalize_repo_patch_payload(
            self._parse_json_provider_payload(response.text, operation_kind="repo_patch"),
            target_file_paths=target_file_paths,
        )
        filesystem_write_idempotency_key = stable_hash(
            context.request_id,
            operation.node_id,
            target_file_paths,
            patch_payload,
        )
        unresolved_launch: SideEffectReceipt | None = None
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in context.state.side_effect_receipts:
            receipt = (SideEffectReceipt).model_validate(receipt_payload)
            if receipt.action_kind != "filesystem_write" or receipt.idempotency_key != filesystem_write_idempotency_key:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.status == "launched":
                unresolved_launch = receipt
        if terminal_receipt is not None:
            result_ref = dict(terminal_receipt.result_ref or {})
            if terminal_receipt.status in {"completed", "reconciled"} and "output" in result_ref:
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status=terminal_receipt.status,
                )
                return result_ref.get("output")
            raise HardInvalidation(
                f"filesystem_write {filesystem_write_idempotency_key[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        applied = not self._filesystem_is_read_only(self.runtime.deployment_contract.filesystem_policy)
        writes: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        for file_update in patch_payload["files"]:
            path = str(file_update["path"]).strip()
            resolved_path = self._resolve_bounded_path(
                path,
                workspace_root=workspace_root,
                operation_kind="repo_patch write",
            )
            path_identity = self._path_identity(resolved_path)
            if path_identity not in resolved_targets:
                raise HardInvalidation(
                    f"repo_patch attempted to write undeclared or out-of-bounds path {path!r}"
                )
            updated_content = str(file_update["updated_content"])
            existing_snapshot = snapshot_by_identity.get(path_identity)
            before_content = str(existing_snapshot.get("content", "")) if existing_snapshot is not None else ""
            before_exists = resolved_path.exists()
            writes.append(
                {
                    "path": str(resolved_targets[path_identity]),
                    "before_exists": before_exists,
                    "before_digest": stable_hash(before_content) if before_exists else "",
                    "after_exists": True,
                    "after_digest": stable_hash(updated_content),
                    "after_content": updated_content,
                }
            )
            updates.append(
                {
                    "path": str(resolved_targets[path_identity]),
                    "applied": applied,
                    "diff": self._unified_diff(str(resolved_targets[path_identity]), before_content, updated_content),
                }
            )
        output = {
            "summary": patch_payload["summary"],
            "updated_files": updates,
            "applied": applied,
        }
        if unresolved_launch is not None:
            reconciliation_state = self._filesystem_write_reconciliation_state(unresolved_launch)
            if reconciliation_state == "completed":
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=unresolved_launch.side_effect_id,
                    action_kind=unresolved_launch.action_kind,
                    reconciliation_status="filesystem_state_matches_intent",
                )
                return dict(unresolved_launch.result_ref or {}).get("output")
            if reconciliation_state != "prewrite_intact":
                raise HardInvalidation("filesystem_write was already launched and must be reconciled before reissue")
        elif applied:
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"filesystem-write.launch.{filesystem_write_idempotency_key[:12]}",
                    action_fingerprint=stable_hash("filesystem_write", writes),
                    idempotency_key=filesystem_write_idempotency_key,
                    action_kind="filesystem_write",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=getattr(context.active_frame, "frame_id", ""),
                    node_id=operation.node_id,
                    branch_id=getattr(context.active_frame, "worker_id", None),
                    trace_context=trace_context,
                    request_digest=stable_hash(context.request_id, operation.node_id, "filesystem_write"),
                    backend=context.runtime_backend,
                    status="launched",
                    result_ref={"output": output, "writes": writes},
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("before_filesystem_write")
            context.raise_if_cancelled()
        if applied:
            write_target: str | None = None
            try:
                for write in writes:
                    write_target = str(write["path"])
                    self._write_text_atomic(Path(write_target), str(write["after_content"]))
            except Exception as exc:
                context.record_side_effect(
                    SideEffectReceipt(
                        side_effect_id=f"filesystem-write.completion.{filesystem_write_idempotency_key[:12]}",
                        action_fingerprint=stable_hash("filesystem_write", writes, "failed"),
                        idempotency_key=filesystem_write_idempotency_key,
                        action_kind="filesystem_write",
                        request_id=context.request_id,
                        plan_id=context.plan.plan_id,
                        frame_id=getattr(context.active_frame, "frame_id", ""),
                        node_id=operation.node_id,
                        branch_id=getattr(context.active_frame, "worker_id", None),
                        trace_context=trace_context,
                        request_digest=stable_hash(context.request_id, operation.node_id, "filesystem_write", "failed"),
                        backend=context.runtime_backend,
                        status="failed",
                        result_ref={"output": output, "writes": writes, "error": str(exc), "failed_path": write_target},
                        replay_policy="reuse_if_completed",
                        reconciliation_policy="strict",
                        created_at=now_ts(),
                    )
                )
                context.publish_checkpoint_boundary("after_filesystem_write")
                raise HardInvalidation(
                    f"repo_patch failed while writing bounded target {write_target!r}: {exc}"
                ) from exc
        context.record_side_effect(
            SideEffectReceipt(
                side_effect_id=f"filesystem-write.completion.{filesystem_write_idempotency_key[:12]}",
                action_fingerprint=stable_hash("filesystem_write", writes if applied else output, "completed"),
                idempotency_key=filesystem_write_idempotency_key,
                action_kind="filesystem_write",
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                frame_id=getattr(context.active_frame, "frame_id", ""),
                node_id=operation.node_id,
                branch_id=getattr(context.active_frame, "worker_id", None),
                trace_context=trace_context,
                request_digest=stable_hash(context.request_id, operation.node_id, "filesystem_write", "completed"),
                backend=context.runtime_backend,
                status="completed",
                result_ref={"output": output, "writes": writes},
                replay_policy="reuse_if_completed",
                reconciliation_policy="strict",
                created_at=now_ts(),
            )
        )
        context.publish_checkpoint_boundary("after_filesystem_write")
        return output
