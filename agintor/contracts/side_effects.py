from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .tracing import OpenAITraceContext

class ReceiptReconciliationRecord(BaseModel):
    status: Literal[
        "reused_terminal_receipt",
        "terminalized_from_handle",
        "terminalized_from_provider_hook",
        "abandoned_by_cancellation",
        "blocked_strict",
        "blocked_best_effort",
    ]
    source: Literal["resume_reconciliation", "branch_cancellation"] = "resume_reconciliation"
    details: Dict[str, Any] = Field(default_factory=dict)
    reconciled_at: float = 0.0


class SideEffectReceipt(BaseModel):
    side_effect_id: str
    action_fingerprint: str
    idempotency_key: str
    action_kind: Literal[
        "tool_launch",
        "tool_completion",
        "provider_request",
        "provider_completion",
        "service_action",
        "filesystem_write",
    ]
    request_id: str = ""
    plan_id: str = ""
    frame_id: str = ""
    node_id: str = ""
    branch_id: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    request_digest: str
    backend: str
    status: Literal["launched", "completed", "failed", "reconciled", "abandoned"] = "launched"
    result_ref: Dict[str, Any] = Field(default_factory=dict)
    replay_policy: str = "reuse_if_completed"
    reconciliation_policy: str = "strict"
    reconciliation: Optional[ReceiptReconciliationRecord] = None
    created_at: float = 0.0


TERMINAL_RECEIPT_STATUSES = frozenset({"completed", "failed", "reconciled", "abandoned"})


TerminalReceiptStatus = Literal["completed", "failed", "reconciled", "abandoned"]


def is_terminal_receipt(receipt: "SideEffectReceipt" | Dict[str, Any]) -> bool:
    normalized = receipt if isinstance(receipt, SideEffectReceipt) else SideEffectReceipt(**dict(receipt))
    return str(normalized.status or "") in TERMINAL_RECEIPT_STATUSES


def terminalize_receipt(
    receipt: "SideEffectReceipt" | Dict[str, Any],
    *,
    status: TerminalReceiptStatus,
    reconciliation_status: Literal[
        "reused_terminal_receipt",
        "terminalized_from_handle",
        "terminalized_from_provider_hook",
        "abandoned_by_cancellation",
        "blocked_strict",
        "blocked_best_effort",
    ],
    reconciliation_source: Literal["resume_reconciliation", "branch_cancellation"],
    reconciliation_details: Optional[Dict[str, Any]] = None,
    result_ref_updates: Optional[Dict[str, Any]] = None,
) -> "SideEffectReceipt":
    normalized = receipt if isinstance(receipt, SideEffectReceipt) else SideEffectReceipt(**dict(receipt))
    if is_terminal_receipt(normalized):
        return normalized
    merged_result_ref = dict(normalized.result_ref or {})
    merged_result_ref.update(dict(result_ref_updates or {}))
    return normalized.model_copy(
        update={
            "status": status,
            "result_ref": merged_result_ref,
            "reconciliation": ReceiptReconciliationRecord(
                status=reconciliation_status,
                source=reconciliation_source,
                details=dict(reconciliation_details or {}),
                reconciled_at=now_ts(),
            ),
        },
        deep=True,
    )
