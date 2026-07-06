from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .providers import ProviderPlan
from .runtime import DeploymentContract, RuntimeIsolationPolicy

class GoalSpec(BaseModel):
    goal_id: str
    raw_prompt: str
    normalized_goal: str
    goal_keywords: List[str] = Field(default_factory=list)
    goal_phrases: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    success_criteria: List[str] = Field(default_factory=list)
    target_families: List[str] = Field(default_factory=list)
    deployment_preferences: Dict[str, Any] = Field(default_factory=dict)
    assumptions: List[str] = Field(default_factory=list)
    amendment_index: int = 0
    amendment_history: List[str] = Field(default_factory=list)


class SuccessCriterion(BaseModel):
    criterion_id: str
    description: str
    required: bool
    priority: int
    measurable_signal: str
    verifier_hint: str
    target_family: str
    weight: float


class SuccessCriteriaBundle(BaseModel):
    bundle_id: str
    goal_id: str
    criteria: List[SuccessCriterion] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class BenchmarkPlan(BaseModel):
    plan_id: str
    goal_id: str
    family_targets: List[str] = Field(default_factory=list)
    train_task_ids: List[str] = Field(default_factory=list)
    proxy_task_ids: List[str] = Field(default_factory=list)
    val_task_ids: List[str] = Field(default_factory=list)
    test_task_ids: List[str] = Field(default_factory=list)
    synthetic_task_ids: List[str] = Field(default_factory=list)
    verifier_bundle_id: str
    frozen: bool = True


class RuntimePlan(BaseModel):
    plan_id: str
    goal_id: str
    runtime_contract_version: str
    runtime_kind: str = "policy_modules"
    runtime_spec_digest: str = ""
    oracle_package_hash: str = ""
    oracle_public_view_hash: str = ""
    oracle_sealed_view_hash: str = ""
    validation_plan_hash: str = ""
    oracle_package_ref: str = ""
    oracle_public_ref: str = ""
    seed_template: str
    mutable_files: List[str] = Field(default_factory=list)
    immutable_manifest: List[str] = Field(default_factory=list)
    runtime_profile: Dict[str, Any] = Field(default_factory=dict)
    provider_plan: ProviderPlan
    tooling_scope: List[str] = Field(default_factory=list)
    deployment_contract: DeploymentContract


class BuildSummary(BaseModel):
    build_id: str
    goal_id: str
    goal_prompt: str
    goal_task_ids: List[str] = Field(default_factory=list)
    goal_spec_path: str
    success_criteria_path: str
    benchmark_plan_path: str
    verifier_bundle_path: str
    runtime_plan_path: str
    deployment_contract_path: str = ""
    workspace: str
    output_runtime_dir: str
    runtime_kind: str = "policy_modules"
    runtime_spec_digest: str = ""
    runtime_plan_spec_digest: str = ""
    oracle_package_hash: str = ""
    oracle_public_view_hash: str = ""
    oracle_sealed_view_hash: str = ""
    validation_plan_hash: str = ""
    oracle_package_ref: str = ""
    oracle_public_ref: str = ""
    history_path: str = ""
    archive_index_path: str = ""
    validation_history_path: str = ""
    stage_failures_path: str = ""
    evidence_ledger_path: str = ""
    paired_comparisons_path: str = ""
    promotion_ledger_path: str = ""
    signal_sufficiency_path: str = ""
    promotion_counts: Dict[str, int] = Field(default_factory=dict)
    decision_counts: Dict[str, int] = Field(default_factory=dict)
    leaderboard_path: str = ""
    leader_runtime_hash: str = ""
    leader_runtime_dir: str = ""
    runtime_contract_version: str = ""
    selection_policy: str = ""
    best_train_score: float
    best_goal_score: float
    best_val_score: float
    accepted_mutations: int
    archive_cells: int
    agintor_provider: str
    runtime_provider: str
    export_bundle_file: str
    export_summary_path: str = ""


class ExportSummary(BaseModel):
    export_id: str
    build_id: str
    goal_id: str
    goal_prompt: str
    runtime_hash: str
    code_hash: str
    runtime_id: str
    runtime_contract_version: str
    runtime_kind: str = "policy_modules"
    runtime_spec_digest: str = ""
    runtime_plan_spec_digest: str = ""
    oracle_package_hash: str = ""
    oracle_public_view_hash: str = ""
    validation_plan_hash: str = ""
    source_runtime_dir: str
    source_runtime_hash: str
    runtime_profile_path: str
    deployment_contract_path: str
    export_bundle_path: str
    leaderboard_path: str = ""
    runtime_plan_path: str = ""


class FactoryChatIdentity(BaseModel):
    chat_id: str
    project_dir: str
    goal_id: str
    runtime_provider: str
    agintor_provider: str
    runtime_backend: str
    runtime_kind: str = "policy_modules"
    runtime_profile_hash: str = ""
    created_at: float = 0.0
    message_count: int = 0
    last_message_id: Optional[str] = None


class FactoryMessage(BaseModel):
    message_id: str
    message_index: int
    parent_message_id: Optional[str] = None
    chat_id: str
    prompt: str
    created_at: float = 0.0
    build_id: str
    leader_runtime_hash: str
    leader_runtime_dir: str = ""
    runtime_kind: str = "policy_modules"
    runtime_spec_digest: str = ""
    oracle_package_hash: str = ""
    goal_spec_path: str = ""
    success_criteria_path: str = ""
    benchmark_plan_path: str = ""
    verifier_bundle_path: str = ""
    runtime_plan_path: str = ""
    deployment_contract_path: str = ""
    export_summary_path: str = ""
    build_summary_path: str = ""
    signal_sufficiency_path: str = ""
