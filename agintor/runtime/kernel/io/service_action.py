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

class ServiceActionIOMixin:
    @staticmethod
    def _decode_service_response_body(raw_body: bytes, content_type: str) -> Any:
        text = raw_body.decode("utf-8", errors="replace")
        if "json" in content_type.lower():
            try:
                return json.loads(text)
            except Exception:
                return text
        return text

    def _execute_service_action_node(
        self,
        context: PolicyContext,
        operation: PlanNode,
        resolved_args: Mapping[str, Any],
        trace_context: OpenAITraceContext | None,
    ) -> Any:
        network_policy = str(self.runtime.deployment_contract.network_policy or "")
        if not self._service_action_allowed(network_policy):
            raise HardInvalidation(
                f"service_action is not permitted under deployment network policy {network_policy!r}"
            )
        url = str(resolved_args.get("url", "") or "").strip()
        method = str(resolved_args.get("method", "GET") or "GET").strip().upper()
        if not url:
            raise HardInvalidation("service_action requires a url")
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise HardInvalidation(f"service_action has unsupported method {method!r}")
        try:
            transport_compatibility = service_action_transport_compatibility(
                url=url,
                service_transport=resolved_args.get(
                    "service_transport",
                    operation.metadata.get("service_transport"),
                ),
                category_hint=operation.metadata.get(
                    "tool_category_hint",
                    operation.metadata.get("service_category_hint"),
                ),
                allowed_tool_categories=operation.allowed_tool_categories,
            )
        except ValueError as exc:
            raise HardInvalidation(str(exc)) from exc
        service_transport = transport_compatibility.transport
        headers = dict(resolved_args.get("headers", {})) if isinstance(resolved_args.get("headers"), Mapping) else {}
        body = resolved_args.get("body")
        timeout_s = float(resolved_args.get("timeout_s", 10.0) or 10.0)
        service_fingerprint = stable_hash("service_action", service_transport, url, method, headers, body, timeout_s)
        service_idempotency_key = stable_hash(
            context.request_id,
            operation.node_id,
            service_transport,
            url,
            method,
            headers,
            body,
            timeout_s,
        )
        unresolved_launch = False
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in context.state.side_effect_receipts:
            receipt = (SideEffectReceipt).model_validate(receipt_payload)
            if receipt.action_kind != "service_action" or receipt.idempotency_key != service_idempotency_key:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.status == "launched":
                unresolved_launch = True
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
                f"service_action {service_idempotency_key[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        if unresolved_launch:
            raise HardInvalidation("service_action was already launched and must be reconciled before reissue")
        data: bytes | None = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body, sort_keys=True).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode("utf-8")
        request_payload = {
            "service_transport": service_transport,
            "url": url,
            "method": method,
            "headers": {str(key): str(value) for key, value in headers.items()},
            "body": body,
            "timeout_s": timeout_s,
        }
        context.record_side_effect(
            SideEffectReceipt(
                side_effect_id=f"service-action.launch.{service_idempotency_key[:12]}",
                action_fingerprint=service_fingerprint,
                idempotency_key=service_idempotency_key,
                action_kind="service_action",
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                frame_id=getattr(context.active_frame, "frame_id", ""),
                node_id=operation.node_id,
                branch_id=getattr(context.active_frame, "worker_id", None),
                trace_context=trace_context,
                request_digest=stable_hash(context.request_id, operation.node_id, request_payload),
                backend=context.runtime_backend,
                status="launched",
                result_ref={"request": request_payload},
                replay_policy="reconcile_before_reissue",
                reconciliation_policy="strict",
                created_at=now_ts(),
            )
        )
        context.publish_checkpoint_boundary("after_service_action_launch")
        context.raise_if_cancelled()
        request = urllib_request.Request(url=url, method=method, headers={str(k): str(v) for k, v in headers.items()}, data=data)
        try:
            with urllib_request.urlopen(request, timeout=timeout_s) as response:
                raw_body = response.read()
                response_headers = dict(response.headers.items())
                output = {
                    "service_transport": service_transport,
                    "url": url,
                    "method": method,
                    "status_code": int(getattr(response, "status", response.getcode())),
                    "headers": response_headers,
                    "body": self._decode_service_response_body(
                        raw_body,
                        str(response_headers.get("Content-Type", "")),
                    ),
                }
        except urllib_error.URLError as exc:
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"service-action.completion.{service_idempotency_key[:12]}",
                    action_fingerprint=service_fingerprint,
                    idempotency_key=service_idempotency_key,
                    action_kind="service_action",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=getattr(context.active_frame, "frame_id", ""),
                    node_id=operation.node_id,
                    branch_id=getattr(context.active_frame, "worker_id", None),
                    trace_context=trace_context,
                    request_digest=stable_hash(context.request_id, operation.node_id, request_payload, str(exc)),
                    backend=context.runtime_backend,
                    status="failed",
                    result_ref={"request": request_payload, "error": str(exc)},
                    replay_policy="reuse_if_completed",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_service_action_completion")
            raise HardInvalidation(f"service_action failed for {url}: {exc}") from exc
        context.record_side_effect(
            SideEffectReceipt(
                side_effect_id=f"service-action.completion.{service_idempotency_key[:12]}",
                action_fingerprint=service_fingerprint,
                idempotency_key=service_idempotency_key,
                action_kind="service_action",
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                frame_id=getattr(context.active_frame, "frame_id", ""),
                node_id=operation.node_id,
                branch_id=getattr(context.active_frame, "worker_id", None),
                trace_context=trace_context,
                request_digest=stable_hash(context.request_id, operation.node_id, request_payload, output),
                backend=context.runtime_backend,
                status="completed",
                result_ref={"request": request_payload, "output": output},
                replay_policy="reuse_if_completed",
                reconciliation_policy="strict",
                created_at=now_ts(),
            )
        )
        context.publish_checkpoint_boundary("after_service_action_completion")
        return output
