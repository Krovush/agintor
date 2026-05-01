from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .benchmarks import SuiteEvaluation
from .runtime import RuntimeDescriptor

class ArchiveEntry(BaseModel):
    code_hash: str
    runtime_hash: str
    scores: Dict[str, float]
    behavior_bin: List[str]
    scope_tag: str
    complexity_bucket: int
    mutable_loc: int
    trace_refs: List[str]


class PredictorObservation(BaseModel):
    family: str
    feature_vector: List[float]
    label_probability: Optional[float] = None
    label_positive_scalar: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MutationCandidate(BaseModel):
    runtime_dir: str
    patch_text: str
    touched_scope: List[str]
    prompt: str
    objective: str


class EvaluationStageResult(BaseModel):
    stage: int
    passed: bool
    reason: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    suite_evaluation: Optional[SuiteEvaluation] = None


class ArchiveRecord(BaseModel):
    objective: str
    key: str
    entry: ArchiveEntry
    runtime_dir: str


class EvolutionHistoryRow(BaseModel):
    step: int
    objective: str
    parent_runtime_hash: str
    child_runtime_hash: Optional[str] = None
    scope: List[str]
    stage_results: List[EvaluationStageResult]
    accepted: bool = False
    inserted_keys: List[str] = Field(default_factory=list)
