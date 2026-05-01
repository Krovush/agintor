from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .execution import OperationSpec

class VerifierSpec(BaseModel):
    verifier_id: str
    verifier_type: str
    artifact_contract: Dict[str, Any] = Field(default_factory=dict)
    tolerance: float = 0.0
    uses_trace: bool = False
    local_only: bool = True
    expected_signal: str


class VerifierBundle(BaseModel):
    bundle_id: str
    plan_id: str
    verifiers: List[VerifierSpec] = Field(default_factory=list)
    checker_chain_defaults: List[str] = Field(default_factory=list)
    frozen: bool = True
    created_from: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkTask(BaseModel):
    task_id: str
    family: Literal["top", "mem", "tool", "e2e"]
    prompt: str
    task_type: str
    symbolic_seeds: List[str] = Field(default_factory=list)
    file_paths: List[str] = Field(default_factory=list)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    context_items: List[Dict[str, Any]] = Field(default_factory=list)
    operations: List[OperationSpec] = Field(default_factory=list)
    expected: Any
    verifier_type: str = "json_exact"
    externally_visible: bool = True
    verification_required: bool = True
    allow_best_effort: bool = False
    transfer_scored: bool = False
    episode_id: Optional[str] = None
    episode_order: int = 0
    proxy_scope_tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class TaskScore(BaseModel):
    s: float
    rho: float
    cvar: float
    utilities: List[float]
    verifier_scores: List[float]
    costs: List[float]
    latencies: List[float]
    faults: List[int]


class SuiteEvaluation(BaseModel):
    runtime_hash: str
    objective_scores: Dict[str, float]
    task_scores: Dict[str, TaskScore]
    family_scores: Dict[str, Dict[str, float]]
    run_results: List[RunResult]
    invalid: bool = False


class ObjectiveKind(str, Enum):
    SINGLE_TASK = "single_task"
    FAMILY = "family"
    FAMILY_ROBUST = "family_robust"
    GLOBAL = "global"
    GLOBAL_ROBUST = "global_robust"


class ObjectiveSpec(BaseModel):
    name: str
    kind: ObjectiveKind
    task_id: Optional[str] = None
    family: Optional[str] = None
