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

class RuntimeManifest(BaseModel):
    runtime_id: str
    version: str
    policy_modules: Dict[str, str]
    mutable_files: List[str]
    immutable_manifest: List[str]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunManifest(BaseModel):
    run_id: str
    run_root: str
    request_id: str = ""
    evaluation_unit_id: str = ""
    request_mode: Literal["benchmark", "user_request", "batch"] = "benchmark"
    runtime_hash: str = ""
    runtime_contract_version: str = ""
    runtime_backend: str = "local"
    task_id: Optional[str] = None
    seed: Optional[int] = None
    trace_context: Optional[OpenAITraceContext] = None
    current_attempt_id: Optional[str] = None
    latest_checkpoint_ref: Optional[str] = None
    lifecycle_state: Literal["running", "paused", "completed", "failed", "cancelled", "pruned"] = "running"
    resumable: bool = False
    prune_eligible: bool = False
    last_failure_kind: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


class AttemptManifest(BaseModel):
    attempt_id: str
    run_id: str
    run_root: str
    sequence_no: int
    launch_kind: Literal["solve", "run_batch", "resume"]
    lifecycle_state: Literal["running", "completed", "paused", "failed", "crashed", "cancelled"] = "running"
    resumed_from_checkpoint_ref: Optional[str] = None
    workspace_root: str
    latest_checkpoint_ref: Optional[str] = None
    failure_kind: Optional[str] = None
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: Optional[float] = None


class KernelManifest(BaseModel):
    runtime_contract_version: str
    package_name: str
    entry_module: str
    files: Dict[str, str] = Field(default_factory=dict)
    capability_flags: List[str] = Field(default_factory=list)


class RuntimeIsolationPolicy(BaseModel):
    timeout_envelope: Dict[str, Any] = Field(default_factory=dict)
    workspace_root: str = ""
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str = "none"
    filesystem_policy: str = "workspace-read-write"
    required_guarantees: List[str] = Field(default_factory=list)
    desired_guarantees: List[str] = Field(default_factory=list)


class DeploymentContract(BaseModel):
    entry_command: str
    runtime_contract_version: str
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str
    filesystem_policy: str
    dependency_digest_set: List[str] = Field(default_factory=list)
    container_image_digest: Optional[str] = None
    capability_flags: List[str] = Field(default_factory=list)
    runtime_isolation_policy: Optional[RuntimeIsolationPolicy] = None
    notes: List[str] = Field(default_factory=list)


class RuntimeDescriptor(BaseModel):
    code_hash: str
    runtime_hash: str
    behavior_bin: List[str]
    interface_diff_mask: str = "0000"
    scope_tag: str
    complexity_bucket: int
    mutable_loc: int
    mutable_ast_nodes: int = 0

    @classmethod
    def from_runtime_hash(
        cls,
        runtime_hash: str,
        behavior_bin: List[str],
        scope_tag: str,
        complexity_bucket: int,
        mutable_loc: int,
        mutable_ast_nodes: int = 0,
        interface_diff_mask: str = "0000",
    ) -> "RuntimeDescriptor":
        return cls(
            code_hash=stable_hash(runtime_hash, behavior_bin, scope_tag, complexity_bucket, mutable_loc),
            runtime_hash=runtime_hash,
            behavior_bin=behavior_bin,
            interface_diff_mask=interface_diff_mask,
            scope_tag=scope_tag,
            complexity_bucket=complexity_bucket,
            mutable_loc=mutable_loc,
            mutable_ast_nodes=mutable_ast_nodes,
        )
