from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .side_effects import SideEffectReceipt
from .state import AttemptSnapshot, RuntimeStateSnapshot, ShellStateSnapshot, StrictPersistenceModel, TraceCursorSnapshot, WorkingMemorySnapshot

CHECKPOINT_ENVELOPE_SCHEMA_VERSION = "agintor.checkpoint-envelope.v4"


class RecoveryFailureKind(str, Enum):
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    CHECKPOINT_CORRUPT = "checkpoint_corrupt"
    REQUEST_MISMATCH = "request_mismatch"
    RUNTIME_CONTRACT_MISMATCH = "runtime_contract_mismatch"
    RUNTIME_HASH_MISMATCH = "runtime_hash_mismatch"
    PLAN_DIGEST_MISMATCH = "plan_digest_mismatch"
    FRAME_RECONSTRUCTION_FAILED = "frame_reconstruction_failed"
    RECEIPT_RECONCILIATION_FAILED = "receipt_reconciliation_failed"


class CheckpointReference(BaseModel):
    ref: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    task_id: str = ""
    seed: int = 0
    request_id: str = ""
    plan_id: str = ""
    checkpoint_id: str = ""
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    checkpoint_count: int = 0
    latest: bool = False
    resume_eligible: bool = True
    resume_ineligibility_reason: Optional[str] = None


class CheckpointEnvelope(StrictPersistenceModel):
    checkpoint_schema_version: Literal["agintor.checkpoint-envelope.v4"] = CHECKPOINT_ENVELOPE_SCHEMA_VERSION
    checkpoint_id: str
    runtime_contract_version: str
    runtime_hash: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str = ""
    request_id: str
    origin_request_id: Optional[str] = None
    selected_checkpoint_ref: Optional[str] = None
    source_checkpoint_ref: Optional[str] = None
    environment_fingerprint_id: Optional[str] = None
    plan_id: str
    task_id: str
    seed: int
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    resume_eligible: bool = True
    resume_ineligibility_reason: Optional[str] = None
    plan_snapshot: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    runtime_state_snapshot: RuntimeStateSnapshot = Field(default_factory=RuntimeStateSnapshot)
    shell_state_snapshot: ShellStateSnapshot = Field(default_factory=ShellStateSnapshot)
    side_effect_ledger: Dict[str, List[SideEffectReceipt]] = Field(default_factory=lambda: {"receipts": []})
    attempt_snapshot: AttemptSnapshot = Field(default_factory=AttemptSnapshot)
    working_state: WorkingMemorySnapshot = Field(default_factory=WorkingMemorySnapshot)
    trace_cursor: TraceCursorSnapshot = Field(default_factory=TraceCursorSnapshot)

    @model_validator(mode="before")
    @classmethod
    def validate_checkpoint_schema_version(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        if "working_state_summary" in values:
            raise ValueError("legacy checkpoint working_state_summary is not accepted by v4 checkpoint envelopes")
        if "checkpoint_schema_version" not in values:
            return values
        schema_version = str(values.get("checkpoint_schema_version") or "").strip()
        if schema_version != CHECKPOINT_ENVELOPE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported checkpoint envelope schema {schema_version!r}; "
                f"expected {CHECKPOINT_ENVELOPE_SCHEMA_VERSION!r}"
            )
        values = dict(values)
        values["checkpoint_schema_version"] = CHECKPOINT_ENVELOPE_SCHEMA_VERSION
        return values

    @classmethod
    def model_validate_persisted(cls, values: Any) -> "CheckpointEnvelope":
        if isinstance(values, dict) and "checkpoint_schema_version" not in values:
            raise ValueError(
                "persisted checkpoint envelopes must include "
                f"checkpoint_schema_version={CHECKPOINT_ENVELOPE_SCHEMA_VERSION!r}"
            )
        return cls.model_validate(values)
