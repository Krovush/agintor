from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .state import LongTermGraphSnapshot, PredictorSnapshot

class RuntimeSessionSeed(BaseModel):
    """Hydration seed for a follow-up message in an existing runtime chat session.

    Carryover is narrow: long-term memory and predictor state persist across
    messages, plus a small condensed short-term recap. Open handles, side-effect
    ledger, in-flight plan, and message board sequence are NOT carried over —
    each message executes a fresh plan.
    """
    session_id: str
    message_index: int
    parent_message_id: Optional[str] = None
    long_term_graph: LongTermGraphSnapshot = Field(default_factory=LongTermGraphSnapshot)
    predictor_snapshot: Optional[PredictorSnapshot] = None
    short_term_carryover: List[Dict[str, Any]] = Field(default_factory=list)


class RuntimeSessionIdentity(BaseModel):
    session_id: str
    runtime_dir: str
    runtime_hash: str
    runtime_backend: str = ""
    created_at: float = 0.0
    message_count: int = 0
    last_message_id: Optional[str] = None


class RuntimeSessionMessage(BaseModel):
    message_id: str
    message_index: int
    parent_message_id: Optional[str] = None
    session_id: str
    request_id: str
    prompt: str
    created_at: float = 0.0
    boundary_state_path: Optional[str] = None
    long_term_graph_path: Optional[str] = None
    predictor_snapshot_path: Optional[str] = None
    result_path: Optional[str] = None
    response_path: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    lifecycle_state: Literal["completed", "paused", "failed", "cancelled"] = "completed"
