from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

class ProviderRole(BaseModel):
    name: str
    api_key_env: Optional[str] = None
    api_key_file_env: Optional[str] = None
    model_map: Dict[str, str] = Field(default_factory=dict)


class ProviderPlan(BaseModel):
    plan_id: str
    agintor_provider: ProviderRole
    runtime_provider: ProviderRole
    runtime_backend: str


class ModelRequest(BaseModel):
    instructions: str
    prompt: str
    model_class: str
    seed: int
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    text: str
    raw: Dict[str, Any] = Field(default_factory=dict)
    model_name: Optional[str] = None
    trace_call_id: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    token_estimate: int = 0
    latency_s: float = 0.0
    dollar_cost: float = 0.0


class ReplayAllocation(BaseModel):
    allocation_key: str
    cursor_start: int = 0
    cursor_end: int = 0
    next_cursor: int = 0
