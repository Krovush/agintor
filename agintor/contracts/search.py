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
from .evidence import OptimizerUpdate, PromotionDecision, PromotionDecisionType, ProgressSignal
from .runtime import RuntimeDescriptor


class SearchContractModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True)


class ArchiveEntry(SearchContractModel):
    code_hash: str
    runtime_hash: str
    scores: Dict[str, float]
    behavior_bin: List[str]
    scope_tag: str
    complexity_bucket: int
    mutable_loc: int
    trace_refs: List[str]
    promotion_type: Optional[PromotionDecisionType] = None
    promotion_decision_ref: Optional[str] = None
    progress_signal_ref: Optional[str] = None
    evidence_contract_id: str = ""
    evidence_digest: str = ""
    oracle_package_hash: str = ""
    runtime_spec_digest: str = ""
    mutation_action_ids: List[str] = Field(default_factory=list)
    promotion_score: Optional[float] = None
    improved_axes: List[str] = Field(default_factory=list)
    regressed_axes: List[str] = Field(default_factory=list)
    tied_axes: List[str] = Field(default_factory=list)


class PredictorObservation(SearchContractModel):
    family: str
    feature_vector: List[float]
    label_probability: Optional[float] = None
    label_positive_scalar: Optional[float] = None
    promotion_type: Optional[PromotionDecisionType] = None
    evidence_contract_id: str = ""
    axis_ids: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MutationCandidate(BaseModel):
    runtime_dir: str
    patch_text: str
    touched_scope: List[str]
    prompt: str
    objective: str


class EvaluationStageResult(SearchContractModel):
    stage: int
    passed: bool
    reason: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    suite_evaluation: Optional[SuiteEvaluation] = None
    progress_signal: Optional[ProgressSignal] = None
    promotion_decision: Optional[PromotionDecision] = None
    promotion_type: Optional[PromotionDecisionType] = None
    promotion_decision_ref: Optional[str] = None
    progress_signal_ref: Optional[str] = None
    evidence_contract_id: str = ""
    oracle_package_hash: str = ""
    runtime_spec_digest: str = ""
    mutation_action_ids: List[str] = Field(default_factory=list)


class ArchiveRecord(SearchContractModel):
    objective: str
    key: str
    entry: ArchiveEntry
    runtime_dir: str
    archive_kind: Literal["capability", "efficiency", "subskill", "preference"] = "capability"
    promotion_type: Optional[PromotionDecisionType] = None
    promotion_decision_ref: Optional[str] = None
    evidence_contract_id: str = ""
    oracle_package_hash: str = ""
    runtime_spec_digest: str = ""


class EvolutionHistoryRow(SearchContractModel):
    step: int
    objective: str
    parent_runtime_hash: str
    child_runtime_hash: Optional[str] = None
    scope: List[str]
    stage_results: List[EvaluationStageResult]
    accepted: bool = False
    inserted_keys: List[str] = Field(default_factory=list)
    promotion_type: Optional[PromotionDecisionType] = None
    promotion_decision_ref: Optional[str] = None
    progress_signal_ref: Optional[str] = None
    evidence_contract_id: str = ""
    evidence_digest: str = ""
    oracle_package_hash: str = ""
    runtime_spec_digest: str = ""
    mutation_action_ids: List[str] = Field(default_factory=list)
    allowed_optimizer_updates: List[OptimizerUpdate] = Field(default_factory=list)
    forbidden_optimizer_updates: List[OptimizerUpdate] = Field(default_factory=list)
    improved_axes: List[str] = Field(default_factory=list)
    regressed_axes: List[str] = Field(default_factory=list)
    tied_axes: List[str] = Field(default_factory=list)
