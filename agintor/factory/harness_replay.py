from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import DeploymentIdentity, ResearchEpochManifest, TaskEnvelope
from ..contracts.harness import CompositeRunPlan, HarnessProtocol, RuntimeDependencyManifest
from ..contracts.harness_actions import SemanticTransactionProposal
from ..contracts.outcomes import PairKey, pair_key_digest
from ..contracts.promotion_proof import EvaluatorOutcomeProofBinding
from ..contracts.run_evidence import assert_no_resolved_credentials
from ..core.identity import canonical_identity_digest, evidence_digest
from ..search.paired_harness import (
    CompiledTaskPlan,
    EvaluatorCallback,
    HarnessEvaluationRequest,
    PairedHarnessSearchConfig,
    ProposalBatchRequest,
    ProposalCallback,
    canonical_pair_keys,
    paired_task_panel_digest,
    run_paired_harness_search,
)
from ..search.promotion import assert_authoritative_outcome_proof
from .harness_service import (
    HarnessFactoryBuildInput,
    HarnessFactoryBuildResult,
    HarnessFactoryExecutionModeError,
    build_harness_factory_release,
)


HARNESS_FACTORY_REPLAY_SCHEMA_VERSION = "harness-factory-search-replay-v1"
HARNESS_FACTORY_REPLAY_PROVENANCE_SCHEMA_VERSION = (
    "harness-factory-search-replay-execution-v1"
)
MAX_REPLAY_MANIFEST_BYTES = 64 * 1024 * 1024
REPLAY_EVIDENCE_DIR = (
    "controlled_development_and_evaluator_evidence",
    "factory_replays",
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_FORBIDDEN_KEY_PREFIXES = (
    "sealed_",
    "private_",
    "hidden_",
    "oracle_private_",
    "gold_",
    "canary_",
)
_FORBIDDEN_KEYS = {
    "answer_key",
    "expected_answer",
    "gold_patch",
    "hidden_checks",
    "hidden_tests",
    "sealed_fixture",
}


class HarnessFactoryReplayError(RuntimeError):
    """Base class for deterministic harness factory replay failures."""


class HarnessFactoryReplayValidationError(HarnessFactoryReplayError, ValueError):
    """Replay evidence or a callback request crossed a frozen identity."""


class HarnessFactoryReplayExhaustedError(HarnessFactoryReplayValidationError):
    """A replay callback was called without a remaining exact row."""


class HarnessFactoryReplayIncompleteError(HarnessFactoryReplayValidationError):
    """A replay completed without consuming every frozen row exactly once."""


class ReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a portable nonempty identifier")
    return normalized


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _iter_scalars(value: Any):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=True)
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _iter_scalars(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _iter_scalars(item)
        return
    yield value


def _scan_replay_value(
    value: Any,
) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=True)
    assert_no_resolved_credentials(value)

    def scan_structure(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = _normalized_key(raw_key)
                if key in _FORBIDDEN_KEYS or key.startswith(_FORBIDDEN_KEY_PREFIXES):
                    raise HarnessFactoryReplayValidationError(
                        f"sealed/private field is forbidden in replay evidence at {path}.{raw_key}"
                    )
                scan_structure(child, f"{path}.{raw_key}")
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for index, child in enumerate(item):
                scan_structure(child, f"{path}[{index}]")

    scan_structure(value, "root")
    for scalar in _iter_scalars(value):
        if not isinstance(scalar, str):
            continue
        if any(pattern.search(scalar) for pattern in _SECRET_PATTERNS):
            raise HarnessFactoryReplayValidationError(
                "replay evidence contains resolved credential material"
            )


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _s1_config_digest(config: PairedHarnessSearchConfig) -> str:
    return evidence_digest(
        {
            "kind": "paired-harness-search-config-v1",
            "config": config.model_dump(mode="json"),
        }
    )


def _protocol_record_digest(protocol: HarnessProtocol) -> str:
    return canonical_identity_digest(
        protocol.model_dump(mode="python", exclude_none=True),
        domain="harness-replay-protocol-record",
    )


def _plan_record_digest(plan: CompositeRunPlan) -> str:
    return canonical_identity_digest(
        plan.model_dump(mode="python", exclude_none=True),
        domain="harness-replay-composite-plan-record",
    )


class ReplayTaskIdentity(ReplayModel):
    task_manifest_id: str
    task_manifest_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    split_manifest_digest: str
    data_state: Literal["development"] = "development"

    @field_validator("task_manifest_id", "epoch_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "task_manifest_digest",
        "epoch_manifest_digest",
        "split_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @classmethod
    def from_task(cls, task: TaskEnvelope) -> "ReplayTaskIdentity":
        if task.data_state != "development":
            raise HarnessFactoryReplayValidationError(
                "sealed-confirmation tasks may not enter factory replay"
            )
        return cls(
            task_manifest_id=task.task_manifest_id,
            task_manifest_digest=task.task_manifest_digest,
            epoch_id=task.epoch_id,
            epoch_manifest_digest=task.epoch_manifest_digest,
            split_manifest_digest=task.split_manifest_digest,
        )


class ProposalBatchRequestIdentity(ReplayModel):
    search_id: str
    step_index: int = Field(ge=0)
    requested_offspring: int = Field(gt=0)
    remaining_candidate_budget: int = Field(gt=0)
    incumbent_id: str
    incumbent_protocol_digest: str
    incumbent_protocol_record_digest: str
    incumbent_anchor_plan_digest: str
    incumbent_anchor_plan_record_digest: str
    anchor_task: ReplayTaskIdentity
    dependency_manifest_digest: str
    retained_transaction_digests: tuple[str, ...]
    deployment_profile_digest: str
    execution_mode: Literal["offline_scripted"] = "offline_scripted"
    live_authorization_digest: None = None

    @field_validator("search_id", "incumbent_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "incumbent_protocol_digest",
        "incumbent_protocol_record_digest",
        "incumbent_anchor_plan_digest",
        "incumbent_anchor_plan_record_digest",
        "dependency_manifest_digest",
        "deployment_profile_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("retained_transaction_digests")
    @classmethod
    def validate_transaction_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _require_digest(item, "retained_transaction_digest") for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("retained replay transaction identities may not duplicate")
        return normalized


def proposal_batch_request_identity(
    request: ProposalBatchRequest,
) -> ProposalBatchRequestIdentity:
    return ProposalBatchRequestIdentity(
        search_id=request.search_id,
        step_index=request.step_index,
        requested_offspring=request.requested_offspring,
        remaining_candidate_budget=request.remaining_candidate_budget,
        incumbent_id=request.incumbent_id,
        incumbent_protocol_digest=request.incumbent_protocol.source_digest(),
        incumbent_protocol_record_digest=_protocol_record_digest(
            request.incumbent_protocol
        ),
        incumbent_anchor_plan_digest=request.incumbent_anchor_plan.compiled_semantic_digest,
        incumbent_anchor_plan_record_digest=_plan_record_digest(
            request.incumbent_anchor_plan
        ),
        anchor_task=ReplayTaskIdentity.from_task(request.anchor_task),
        dependency_manifest_digest=request.dependency_manifest.manifest_digest(),
        retained_transaction_digests=tuple(
            transaction.transaction_record_digest
            for transaction in request.retained_transactions
        ),
        deployment_profile_digest=request.deployment_profile_digest,
        execution_mode=request.execution_mode,
        live_authorization_digest=request.live_authorization_digest,
    )


def proposal_batch_request_digest(request: ProposalBatchRequest) -> str:
    identity = proposal_batch_request_identity(request)
    return evidence_digest(
        {
            "kind": "harness-replay-proposal-batch-request-v1",
            "request": identity.model_dump(mode="python"),
        }
    )


class ProposalReplayRow(ReplayModel):
    sequence_no: int = Field(ge=0)
    request: ProposalBatchRequestIdentity
    request_digest: str
    proposals: tuple[SemanticTransactionProposal, ...] = Field(min_length=1)
    row_digest: str = ""

    @field_validator("request_digest")
    @classmethod
    def validate_request_digest(cls, value: str) -> str:
        return _require_digest(value, "request_digest")

    @model_validator(mode="after")
    def validate_row(self) -> "ProposalReplayRow":
        computed_request = evidence_digest(
            {
                "kind": "harness-replay-proposal-batch-request-v1",
                "request": self.request.model_dump(mode="python"),
            }
        )
        if self.request_digest != computed_request:
            raise ValueError("proposal replay request digest mismatch")
        if len(self.proposals) != self.request.requested_offspring:
            raise ValueError("proposal replay row must contain the exact frozen batch size")
        transaction_ids = [proposal.transaction_id for proposal in self.proposals]
        if len(transaction_ids) != len(set(transaction_ids)):
            raise ValueError("proposal replay batch transaction ids may not duplicate")
        for proposal in self.proposals:
            crossed = []
            if proposal.parent_source_protocol_digest != self.request.incumbent_protocol_digest:
                crossed.append("parent_source_protocol_digest")
            if proposal.parent_compiled_semantic_digest != self.request.incumbent_anchor_plan_digest:
                crossed.append("parent_compiled_semantic_digest")
            if proposal.task_envelope_digest != self.request.anchor_task.task_manifest_digest:
                crossed.append("task_envelope_digest")
            if proposal.dependency_manifest_digest != self.request.dependency_manifest_digest:
                crossed.append("dependency_manifest_digest")
            if crossed:
                raise ValueError(
                    "proposal replay row crossed request identity: " + ", ".join(crossed)
                )
        computed_row = evidence_digest(
            {
                "kind": "harness-replay-proposal-row-v1",
                "row": self.model_dump(mode="python", exclude={"row_digest"}),
            }
        )
        if self.row_digest and self.row_digest != computed_row:
            raise ValueError("proposal replay row digest mismatch")
        if not self.row_digest:
            object.__setattr__(self, "row_digest", computed_row)
        return self


class CompiledTaskPlanIdentity(ReplayModel):
    task_manifest_id: str
    task_manifest_digest: str
    source_protocol_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    plan_record_digest: str

    @field_validator("task_manifest_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_identifier(value, "task_manifest_id")

    @field_validator(
        "task_manifest_digest",
        "source_protocol_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "plan_record_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @classmethod
    def from_plan(cls, value: CompiledTaskPlan) -> "CompiledTaskPlanIdentity":
        return cls(
            task_manifest_id=value.task_manifest_id,
            task_manifest_digest=value.task_manifest_digest,
            source_protocol_digest=value.plan.source_protocol_digest,
            compiled_semantic_digest=value.plan.compiled_semantic_digest,
            dependency_manifest_digest=value.plan.dependency_manifest_digest,
            plan_record_digest=_plan_record_digest(value.plan),
        )


class HarnessEvaluationRequestIdentity(ReplayModel):
    evaluation_id: str
    arm_id: str
    arm_kind: Literal["search_parent", "search_child", "control"]
    control_kind: str | None = None
    opportunity_index: int = Field(ge=0)
    protocol_digest: str
    protocol_record_digest: str
    compiled_plans: tuple[CompiledTaskPlanIdentity, ...] = Field(min_length=1)
    expected_pair_keys: tuple[PairKey, ...] = Field(min_length=1)
    expected_pair_panel_digest: str
    deployment_profile_digest: str
    execution_mode: Literal["offline_scripted"] = "offline_scripted"
    live_authorization_digest: None = None

    @field_validator("evaluation_id", "arm_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "protocol_digest",
        "protocol_record_digest",
        "expected_pair_panel_digest",
        "deployment_profile_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_identity(self) -> "HarnessEvaluationRequestIdentity":
        if self.arm_kind == "control" and not self.control_kind:
            raise ValueError("control evaluation replay identity requires control_kind")
        if self.arm_kind != "control" and self.control_kind is not None:
            raise ValueError("non-control evaluation replay identity may not set control_kind")
        plan_ids = [plan.task_manifest_id for plan in self.compiled_plans]
        if plan_ids != sorted(plan_ids) or len(plan_ids) != len(set(plan_ids)):
            raise ValueError("compiled replay task plans must be unique and sorted")
        if any(plan.source_protocol_digest != self.protocol_digest for plan in self.compiled_plans):
            raise ValueError("compiled replay plan crossed evaluation protocol")
        canonical_pairs = canonical_pair_keys(self.expected_pair_keys)
        if canonical_pairs != self.expected_pair_keys:
            raise ValueError("evaluation replay PairKeys must be canonical")
        if paired_task_panel_digest(canonical_pairs) != self.expected_pair_panel_digest:
            raise ValueError("evaluation replay PairKey panel digest mismatch")
        if {pair.task_manifest_id for pair in canonical_pairs} != set(plan_ids):
            raise ValueError("evaluation replay PairKeys do not exactly cover compiled task plans")
        return self


def harness_evaluation_request_identity(
    request: HarnessEvaluationRequest,
) -> HarnessEvaluationRequestIdentity:
    if (
        request.execution_mode != "offline_scripted"
        or request.live_authorization_digest is not None
    ):
        raise HarnessFactoryReplayValidationError(
            "live-authorized evaluator requests cannot enter deterministic replay"
        )
    return HarnessEvaluationRequestIdentity(
        evaluation_id=request.evaluation_id,
        arm_id=request.arm_id,
        arm_kind=request.arm_kind,
        control_kind=request.control_kind,
        opportunity_index=request.opportunity_index,
        protocol_digest=request.protocol.source_digest(),
        protocol_record_digest=_protocol_record_digest(request.protocol),
        compiled_plans=tuple(
            CompiledTaskPlanIdentity.from_plan(plan)
            for plan in request.compiled_plans
        ),
        expected_pair_keys=tuple(request.expected_pair_keys),
        expected_pair_panel_digest=paired_task_panel_digest(request.expected_pair_keys),
        deployment_profile_digest=request.deployment_profile_digest,
        execution_mode=request.execution_mode,
        live_authorization_digest=request.live_authorization_digest,
    )


def harness_evaluation_request_digest(request: HarnessEvaluationRequest) -> str:
    identity = harness_evaluation_request_identity(request)
    return evidence_digest(
        {
            "kind": "harness-replay-evaluation-request-v1",
            "request": identity.model_dump(mode="python"),
        }
    )


class EvaluatorReplayRow(ReplayModel):
    sequence_no: int = Field(ge=0)
    request: HarnessEvaluationRequestIdentity
    request_digest: str
    proof_bindings: tuple[EvaluatorOutcomeProofBinding, ...] = Field(min_length=1)
    row_digest: str = ""

    @field_validator("request_digest")
    @classmethod
    def validate_request_digest(cls, value: str) -> str:
        return _require_digest(value, "request_digest")

    @model_validator(mode="after")
    def validate_row(self) -> "EvaluatorReplayRow":
        computed_request = evidence_digest(
            {
                "kind": "harness-replay-evaluation-request-v1",
                "request": self.request.model_dump(mode="python"),
            }
        )
        if self.request_digest != computed_request:
            raise ValueError("evaluator replay request digest mismatch")
        expected_pairs = self.request.expected_pair_keys
        if tuple(
            binding.outcome_receipt.pair_key for binding in self.proof_bindings
        ) != expected_pairs:
            raise ValueError(
                "evaluator replay proofs must exactly follow canonical PairKey order"
            )
        plan_by_task = {
            plan.task_manifest_id: plan for plan in self.request.compiled_plans
        }
        for binding in self.proof_bindings:
            receipt = binding.outcome_receipt
            run = binding.run_evidence
            plan = plan_by_task[receipt.task_manifest_id]
            crossed = []
            if receipt.task_manifest_digest != plan.task_manifest_digest:
                crossed.append("task_manifest_digest")
            if receipt.protocol_digest != self.request.protocol_digest:
                crossed.append("protocol_digest")
            if run.compiled_semantic_digest != plan.compiled_semantic_digest:
                crossed.append("compiled_semantic_digest")
            if run.dependency_manifest_digest != plan.dependency_manifest_digest:
                crossed.append("dependency_manifest_digest")
            if crossed:
                raise ValueError(
                    "evaluator replay receipt crossed request identity: " + ", ".join(crossed)
                )
        receipt_digests = [
            binding.outcome_receipt.receipt_digest
            for binding in self.proof_bindings
        ]
        if len(receipt_digests) != len(set(receipt_digests)):
            raise ValueError("evaluator replay receipt identities may not duplicate within a panel")
        binding_digests = [binding.binding_digest for binding in self.proof_bindings]
        if len(binding_digests) != len(set(binding_digests)):
            raise ValueError("evaluator replay proof identities may not duplicate within a panel")
        computed_row = evidence_digest(
            {
                "kind": "harness-replay-evaluator-row-v1",
                "row": self.model_dump(mode="python", exclude={"row_digest"}),
            }
        )
        if self.row_digest and self.row_digest != computed_row:
            raise ValueError("evaluator replay row digest mismatch")
        if not self.row_digest:
            object.__setattr__(self, "row_digest", computed_row)
        return self


class HarnessFactoryReplayManifest(ReplayModel):
    schema_version: Literal[HARNESS_FACTORY_REPLAY_SCHEMA_VERSION] = (
        HARNESS_FACTORY_REPLAY_SCHEMA_VERSION
    )
    manifest_id: str
    manifest_digest: str = ""
    execution_mode: Literal["deterministic_replay"] = "deterministic_replay"
    live_inference_status: Literal["not_run"] = "not_run"
    real_inference_requests_sent: Literal[0] = 0
    epoch_id: str
    epoch_manifest_digest: str
    development_split_digest: str
    task_panel_digest: str
    task_panel: tuple[ReplayTaskIdentity, ...] = Field(min_length=1)
    deployment: DeploymentIdentity
    evaluator_id: str
    evaluator_identity_digest: str
    evaluation_policy_digest: str
    dependency_manifest: RuntimeDependencyManifest
    dependency_manifest_digest: str
    founding_protocol_digest: str
    founding_protocol_record_digest: str
    s1_config: PairedHarnessSearchConfig
    s1_config_digest: str
    proposal_rows: tuple[ProposalReplayRow, ...] = Field(min_length=1)
    evaluator_rows: tuple[EvaluatorReplayRow, ...] = Field(min_length=1)

    @field_validator("manifest_id", "epoch_id", "evaluator_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "epoch_manifest_digest",
        "development_split_digest",
        "task_panel_digest",
        "evaluator_identity_digest",
        "evaluation_policy_digest",
        "dependency_manifest_digest",
        "founding_protocol_digest",
        "founding_protocol_record_digest",
        "s1_config_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_manifest(self) -> "HarnessFactoryReplayManifest":
        if self.dependency_manifest_digest != self.dependency_manifest.manifest_digest():
            raise ValueError("replay dependency manifest digest mismatch")
        if self.s1_config.execution_mode != "offline_scripted":
            raise ValueError("factory replay requires an offline_scripted S1 config")
        if self.s1_config.live_authorization is not None:
            raise ValueError("factory replay cannot bind a live-authorized S1 config")
        if self.s1_config_digest != _s1_config_digest(self.s1_config):
            raise ValueError("factory replay S1 config digest mismatch")
        tasks = tuple(task.task_manifest_id for task in self.task_panel)
        if tasks != tuple(sorted(tasks)) or len(tasks) != len(set(tasks)):
            raise ValueError("factory replay task panel must be unique and sorted")
        if any(
            task.epoch_id != self.epoch_id
            or task.epoch_manifest_digest != self.epoch_manifest_digest
            or task.split_manifest_digest != self.development_split_digest
            for task in self.task_panel
        ):
            raise ValueError("factory replay task panel crossed epoch authority")
        expected_pairs = self.s1_config.expected_pair_keys
        if paired_task_panel_digest(expected_pairs) != self.task_panel_digest:
            raise ValueError("factory replay task panel digest crossed S1 PairKeys")
        if {pair.task_manifest_id for pair in expected_pairs} != set(tasks):
            raise ValueError("factory replay S1 PairKeys do not exactly cover tasks")
        if any(
            pair.provider_config_digest != self.deployment.provider_config_digest
            for pair in expected_pairs
        ):
            raise ValueError("factory replay PairKeys crossed deployment configuration")

        proposal_sequences = [row.sequence_no for row in self.proposal_rows]
        evaluator_sequences = [row.sequence_no for row in self.evaluator_rows]
        if proposal_sequences != list(range(len(self.proposal_rows))):
            raise ValueError("proposal replay rows must be contiguous and ordered")
        if evaluator_sequences != list(range(len(self.evaluator_rows))):
            raise ValueError("evaluator replay rows must be contiguous and ordered")
        proposal_request_digests = [row.request_digest for row in self.proposal_rows]
        evaluator_request_digests = [row.request_digest for row in self.evaluator_rows]
        if len(proposal_request_digests) != len(set(proposal_request_digests)):
            raise ValueError("proposal replay requests may not duplicate")
        if len(evaluator_request_digests) != len(set(evaluator_request_digests)):
            raise ValueError("evaluator replay requests may not duplicate")
        if self.proposal_rows[0].request.incumbent_protocol_digest != self.founding_protocol_digest:
            raise ValueError("first proposal replay request crossed founding protocol")
        anchor_task = self.task_panel[0]
        for row in self.proposal_rows:
            if row.request.search_id != self.s1_config.search_id:
                raise ValueError("proposal replay row crossed S1 search identity")
            if row.request.anchor_task != anchor_task:
                raise ValueError("proposal replay row crossed canonical anchor task")
            if row.request.dependency_manifest_digest != self.dependency_manifest_digest:
                raise ValueError("proposal replay row crossed runtime dependencies")
            if (
                row.request.deployment_profile_digest
                != self.s1_config.deployment_profile_digest
            ):
                raise ValueError("proposal replay row crossed deployment profile")

        if self.evaluator_rows[0].request.arm_kind != "search_parent":
            raise ValueError("first evaluator replay row must be the founding search parent")
        task_by_id = {task.task_manifest_id: task for task in self.task_panel}
        receipt_digests: set[str] = set()
        evaluation_contracts: set[tuple[str, str]] = set()
        for row in self.evaluator_rows:
            request = row.request
            if request.expected_pair_keys != expected_pairs:
                raise ValueError("evaluator replay row crossed S1 PairKeys")
            if (
                request.deployment_profile_digest
                != self.s1_config.deployment_profile_digest
            ):
                raise ValueError("evaluator replay row crossed deployment profile")
            if any(
                plan.dependency_manifest_digest != self.dependency_manifest_digest
                for plan in request.compiled_plans
            ):
                raise ValueError("evaluator replay compiled plan crossed dependencies")
            for binding in row.proof_bindings:
                receipt = binding.outcome_receipt
                run = binding.run_evidence
                task = task_by_id[receipt.task_manifest_id]
                crossed = []
                if receipt.data_state != "development":
                    crossed.append("data_state")
                if receipt.epoch_id != self.epoch_id:
                    crossed.append("epoch_id")
                if receipt.epoch_manifest_digest != self.epoch_manifest_digest:
                    crossed.append("epoch_manifest_digest")
                if receipt.split_manifest_digest != self.development_split_digest:
                    crossed.append("split_manifest_digest")
                if receipt.task_manifest_digest != task.task_manifest_digest:
                    crossed.append("task_manifest_digest")
                if receipt.evaluator_id != self.evaluator_id:
                    crossed.append("evaluator_id")
                if receipt.evaluator_identity_digest != self.evaluator_identity_digest:
                    crossed.append("evaluator_identity_digest")
                if receipt.evaluation_policy_digest != self.evaluation_policy_digest:
                    crossed.append("evaluation_policy_digest")
                if receipt.pair_key.provider_config_digest != self.deployment.provider_config_digest:
                    crossed.append("provider_config_digest")
                if receipt.protocol_digest != request.protocol_digest:
                    crossed.append("protocol_digest")
                if receipt.compiler_digest != self.dependency_manifest.compiler.implementation_digest:
                    crossed.append("compiler_digest")
                if receipt.kernel_digest != self.dependency_manifest.kernel.implementation_digest:
                    crossed.append("kernel_digest")
                if receipt.tool_manifest_digest != self.dependency_manifest_digest:
                    crossed.append("tool_manifest_digest")
                if run.deployment_id != self.deployment.deployment_id:
                    crossed.append("deployment_id")
                if run.provider != self.deployment.provider:
                    crossed.append("provider")
                if run.model != self.deployment.model:
                    crossed.append("model")
                for field_name in (
                    "provider_config_digest",
                    "decoding_policy_digest",
                    "price_schedule_digest",
                    "command_container_policy_digest",
                ):
                    if getattr(run, field_name) != getattr(self.deployment, field_name):
                        crossed.append(field_name)
                if crossed:
                    raise ValueError(
                        "evaluator replay receipt crossed manifest authority: "
                        + ", ".join(crossed)
                    )
                if receipt.receipt_digest in receipt_digests:
                    raise ValueError("evaluator receipt was reused across replay rows")
                receipt_digests.add(receipt.receipt_digest)
                evaluation_contracts.add(
                    (receipt.evaluation_contract_id, receipt.evaluation_contract_digest)
                )
        if len(evaluation_contracts) != 1:
            raise ValueError("factory replay must use one frozen evaluator contract")

        proposal_ids = [
            proposal.transaction_id
            for row in self.proposal_rows
            for proposal in row.proposals
        ]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("proposal transaction identity was reused across replay rows")
        _scan_replay_value(self.model_dump(mode="python", exclude={"manifest_digest"}))
        computed = evidence_digest(
            {
                "kind": HARNESS_FACTORY_REPLAY_SCHEMA_VERSION,
                "manifest": self.model_dump(mode="python", exclude={"manifest_digest"}),
            }
        )
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("harness factory replay manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


def build_harness_factory_replay_manifest(
    *,
    manifest_id: str,
    build_input: HarnessFactoryBuildInput,
    proposal_rows: Sequence[ProposalReplayRow],
    evaluator_rows: Sequence[EvaluatorReplayRow],
) -> HarnessFactoryReplayManifest:
    if build_input.execution_mode != "offline_scripted":
        raise HarnessFactoryReplayValidationError(
            "replay manifests require an offline_scripted factory build input"
        )
    tasks = tuple(
        ReplayTaskIdentity.from_task(task)
        for task in sorted(build_input.task_panel, key=lambda item: item.task_manifest_id)
    )
    epoch = build_input.epoch
    manifest = HarnessFactoryReplayManifest(
        manifest_id=manifest_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        development_split_digest=epoch.development_split_digest,
        task_panel_digest=epoch.search_envelope.task_panel_digest,
        task_panel=tasks,
        deployment=epoch.deployment,
        evaluator_id=epoch.evaluator_authority.evaluator_id,
        evaluator_identity_digest=epoch.evaluator_authority.evaluator_identity_digest,
        evaluation_policy_digest=epoch.evaluator_authority.evaluation_policy_digest,
        dependency_manifest=build_input.dependency_manifest,
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        founding_protocol_digest=build_input.founding_protocol.source_digest(),
        founding_protocol_record_digest=_protocol_record_digest(
            build_input.founding_protocol
        ),
        s1_config=build_input.s1_config,
        s1_config_digest=_s1_config_digest(build_input.s1_config),
        proposal_rows=tuple(proposal_rows),
        evaluator_rows=tuple(evaluator_rows),
    )
    _scan_replay_value(manifest)
    validate_harness_factory_replay_bindings(manifest, build_input=build_input)
    return manifest


def validate_harness_factory_replay_bindings(
    manifest: HarnessFactoryReplayManifest,
    *,
    build_input: HarnessFactoryBuildInput,
) -> None:
    manifest = HarnessFactoryReplayManifest.model_validate(
        manifest.model_dump(mode="python")
    )
    epoch = build_input.epoch
    if build_input.execution_mode != "offline_scripted":
        raise HarnessFactoryExecutionModeError(
            "factory replay can run only through offline_scripted service mode"
        )
    expected = {
        "epoch_id": epoch.epoch_id,
        "epoch_manifest_digest": epoch.epoch_manifest_digest,
        "development_split_digest": epoch.development_split_digest,
        "task_panel_digest": epoch.search_envelope.task_panel_digest,
        "dependency_manifest_digest": build_input.dependency_manifest.manifest_digest(),
        "founding_protocol_digest": build_input.founding_protocol.source_digest(),
        "founding_protocol_record_digest": _protocol_record_digest(
            build_input.founding_protocol
        ),
        "s1_config_digest": _s1_config_digest(build_input.s1_config),
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(manifest, field_name) != expected_value
    ]
    if crossed:
        raise HarnessFactoryReplayValidationError(
            "factory replay crossed build input: " + ", ".join(crossed)
        )
    if manifest.deployment != epoch.deployment:
        raise HarnessFactoryReplayValidationError(
            "factory replay crossed deployment identity"
        )
    if manifest.dependency_manifest != build_input.dependency_manifest:
        raise HarnessFactoryReplayValidationError(
            "factory replay crossed dependency manifest"
        )
    if manifest.s1_config != build_input.s1_config:
        raise HarnessFactoryReplayValidationError("factory replay crossed S1 config")
    task_panel = tuple(
        ReplayTaskIdentity.from_task(task)
        for task in sorted(build_input.task_panel, key=lambda item: item.task_manifest_id)
    )
    if manifest.task_panel != task_panel:
        raise HarnessFactoryReplayValidationError(
            "factory replay crossed exact task panel identities"
        )
    evaluator = epoch.evaluator_authority
    if (
        manifest.evaluator_id != evaluator.evaluator_id
        or manifest.evaluator_identity_digest != evaluator.evaluator_identity_digest
        or manifest.evaluation_policy_digest != evaluator.evaluation_policy_digest
    ):
        raise HarnessFactoryReplayValidationError(
            "factory replay crossed evaluator authority"
        )
    task_by_id = {task.task_manifest_id: task for task in build_input.task_panel}
    for row in manifest.evaluator_rows:
        for binding in row.proof_bindings:
            receipt = binding.outcome_receipt
            try:
                assert_authoritative_outcome_proof(
                    binding,
                    epoch,
                    expected_profile_digest=build_input.s1_config.deployment_profile_digest,
                )
            except Exception as exc:
                raise HarnessFactoryReplayValidationError(
                    "factory replay proof is not authoritative for the epoch"
                ) from exc
            task = task_by_id[receipt.task_manifest_id]
            if receipt.task_manifest_digest != task.task_manifest_digest:
                raise HarnessFactoryReplayValidationError(
                    "factory replay receipt crossed task manifest"
                )


class ReplayProposalCallback:
    def __init__(self, manifest: HarnessFactoryReplayManifest) -> None:
        self._rows = manifest.proposal_rows
        self._cursor = 0

    @property
    def consumed(self) -> int:
        return self._cursor

    @property
    def total(self) -> int:
        return len(self._rows)

    def __call__(
        self,
        request: ProposalBatchRequest,
    ) -> tuple[SemanticTransactionProposal, ...]:
        if self._cursor >= len(self._rows):
            raise HarnessFactoryReplayExhaustedError(
                "proposal replay has no remaining row; request was reused or extra"
            )
        row = self._rows[self._cursor]
        identity = proposal_batch_request_identity(request)
        digest = proposal_batch_request_digest(request)
        if identity != row.request or digest != row.request_digest:
            raise HarnessFactoryReplayValidationError(
                f"proposal replay request order/identity mismatch at row {self._cursor}"
            )
        self._cursor += 1
        return row.proposals

    def assert_reconciled(self) -> None:
        if self._cursor != len(self._rows):
            raise HarnessFactoryReplayIncompleteError(
                f"proposal replay left {len(self._rows) - self._cursor} unconsumed rows"
            )


class ReplayEvaluatorCallback:
    def __init__(self, manifest: HarnessFactoryReplayManifest) -> None:
        self._rows = manifest.evaluator_rows
        self._cursor = 0

    @property
    def consumed(self) -> int:
        return self._cursor

    @property
    def total(self) -> int:
        return len(self._rows)

    def __call__(
        self,
        request: HarnessEvaluationRequest,
    ) -> tuple[EvaluatorOutcomeProofBinding, ...]:
        if self._cursor >= len(self._rows):
            raise HarnessFactoryReplayExhaustedError(
                "evaluator replay has no remaining row; request was reused or extra"
            )
        row = self._rows[self._cursor]
        identity = harness_evaluation_request_identity(request)
        digest = harness_evaluation_request_digest(request)
        if identity != row.request or digest != row.request_digest:
            raise HarnessFactoryReplayValidationError(
                f"evaluator replay request order/identity mismatch at row {self._cursor}"
            )
        self._cursor += 1
        return row.proof_bindings

    def assert_reconciled(self) -> None:
        if self._cursor != len(self._rows):
            raise HarnessFactoryReplayIncompleteError(
                f"evaluator replay left {len(self._rows) - self._cursor} unconsumed rows"
            )


class HarnessFactoryReplayCallbacks:
    def __init__(self, manifest: HarnessFactoryReplayManifest) -> None:
        self.proposal = ReplayProposalCallback(manifest)
        self.evaluator = ReplayEvaluatorCallback(manifest)

    def assert_reconciled(self) -> None:
        self.proposal.assert_reconciled()
        self.evaluator.assert_reconciled()


class HarnessFactoryReplayRecorder:
    """Author an immutable transcript around already-explicit scripted callbacks."""

    def __init__(
        self,
        *,
        build_input: HarnessFactoryBuildInput,
        proposal_callback: ProposalCallback,
        evaluator_callback: EvaluatorCallback,
    ) -> None:
        self.build_input = build_input
        self._proposal_source = proposal_callback
        self._evaluator_source = evaluator_callback
        self._proposal_rows: list[ProposalReplayRow] = []
        self._evaluator_rows: list[EvaluatorReplayRow] = []

    def proposal_callback(
        self,
        request: ProposalBatchRequest,
    ) -> tuple[SemanticTransactionProposal, ...]:
        proposals = tuple(self._proposal_source(request))
        row = ProposalReplayRow(
            sequence_no=len(self._proposal_rows),
            request=proposal_batch_request_identity(request),
            request_digest=proposal_batch_request_digest(request),
            proposals=proposals,
        )
        self._proposal_rows.append(row)
        return proposals

    def evaluator_callback(
        self,
        request: HarnessEvaluationRequest,
    ) -> tuple[EvaluatorOutcomeProofBinding, ...]:
        proof_bindings = tuple(self._evaluator_source(request))
        row = EvaluatorReplayRow(
            sequence_no=len(self._evaluator_rows),
            request=harness_evaluation_request_identity(request),
            request_digest=harness_evaluation_request_digest(request),
            proof_bindings=proof_bindings,
        )
        self._evaluator_rows.append(row)
        return proof_bindings

    def manifest(
        self,
        *,
        manifest_id: str,
    ) -> HarnessFactoryReplayManifest:
        return build_harness_factory_replay_manifest(
            manifest_id=manifest_id,
            build_input=self.build_input,
            proposal_rows=self._proposal_rows,
            evaluator_rows=self._evaluator_rows,
        )


class HarnessFactoryReplayExecutionProvenance(ReplayModel):
    schema_version: Literal[HARNESS_FACTORY_REPLAY_PROVENANCE_SCHEMA_VERSION] = (
        HARNESS_FACTORY_REPLAY_PROVENANCE_SCHEMA_VERSION
    )
    provenance_digest: str = ""
    execution_mode: Literal["deterministic_replay"] = "deterministic_replay"
    live_inference_status: Literal["not_run"] = "not_run"
    real_inference_requests_sent: Literal[0] = 0
    provider_invocation_receipt_digests: tuple[str, ...] = ()
    replay_manifest_id: str
    replay_manifest_digest: str
    epoch_manifest_digest: str
    task_panel_digest: str
    dependency_manifest_digest: str
    founding_protocol_digest: str
    s1_config_digest: str
    proposal_row_digests: tuple[str, ...] = Field(min_length=1)
    evaluator_row_digests: tuple[str, ...] = Field(min_length=1)
    proposal_rows_consumed: int = Field(gt=0)
    evaluator_rows_consumed: int = Field(gt=0)
    reconciliation_complete: Literal[True] = True
    search_result_digest: str
    release_digest: str
    release_manifest_digest: str
    selected_protocol_digest: str

    @field_validator("replay_manifest_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_identifier(value, "replay_manifest_id")

    @field_validator(
        "replay_manifest_digest",
        "epoch_manifest_digest",
        "task_panel_digest",
        "dependency_manifest_digest",
        "founding_protocol_digest",
        "s1_config_digest",
        "search_result_digest",
        "release_digest",
        "release_manifest_digest",
        "selected_protocol_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("proposal_row_digests", "evaluator_row_digests")
    @classmethod
    def validate_row_digests(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(_require_digest(item, info.field_name) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} may not duplicate")
        return normalized

    @field_validator("provider_invocation_receipt_digests")
    @classmethod
    def reject_provider_receipts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError(
                "deterministic factory replay cannot contain provider invocation receipts"
            )
        return value

    @model_validator(mode="after")
    def bind_provenance(self) -> "HarnessFactoryReplayExecutionProvenance":
        if self.proposal_rows_consumed != len(self.proposal_row_digests):
            raise ValueError("proposal replay provenance count mismatch")
        if self.evaluator_rows_consumed != len(self.evaluator_row_digests):
            raise ValueError("evaluator replay provenance count mismatch")
        computed = evidence_digest(
            {
                "kind": HARNESS_FACTORY_REPLAY_PROVENANCE_SCHEMA_VERSION,
                "provenance": self.model_dump(
                    mode="python",
                    exclude={"provenance_digest"},
                ),
            }
        )
        if self.provenance_digest and self.provenance_digest != computed:
            raise ValueError("factory replay execution provenance digest mismatch")
        if not self.provenance_digest:
            object.__setattr__(self, "provenance_digest", computed)
        return self


class HarnessFactoryReplayBuildResult(ReplayModel):
    service_result: HarnessFactoryBuildResult
    provenance: HarnessFactoryReplayExecutionProvenance
    provenance_path: str

    @field_validator("provenance_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("provenance_path may not be empty")
        return str(value)


def write_harness_factory_replay_manifest(
    path: str | Path,
    manifest: HarnessFactoryReplayManifest,
) -> Path:
    destination = Path(path).expanduser().resolve()
    if destination.suffix.casefold() != ".json":
        raise HarnessFactoryReplayValidationError(
            "factory replay manifest path must be a JSON file"
        )
    if destination.is_symlink():
        raise HarnessFactoryReplayValidationError(
            "factory replay manifest path may not be a symlink"
        )
    _scan_replay_value(manifest)
    raw = _canonical_bytes(manifest.model_dump(mode="json", exclude_none=True))
    if len(raw) > MAX_REPLAY_MANIFEST_BYTES:
        raise HarnessFactoryReplayValidationError(
            "factory replay manifest exceeds the maximum serialized size"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = load_harness_factory_replay_manifest(destination)
        if existing != manifest or destination.read_bytes() != raw:
            raise FileExistsError(
                "refusing to overwrite a different immutable factory replay manifest"
            )
        return destination
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    loaded = load_harness_factory_replay_manifest(destination)
    if loaded != manifest:
        raise HarnessFactoryReplayValidationError(
            "immutable factory replay failed write verification"
        )
    return destination


def load_harness_factory_replay_manifest(
    path: str | Path,
) -> HarnessFactoryReplayManifest:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(f"factory replay manifest is missing: {source}")
    raw = source.read_bytes()
    if len(raw) > MAX_REPLAY_MANIFEST_BYTES:
        raise HarnessFactoryReplayValidationError(
            "factory replay manifest exceeds the maximum serialized size"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessFactoryReplayValidationError(
            "factory replay manifest must be valid UTF-8 JSON"
        ) from exc
    manifest = HarnessFactoryReplayManifest.model_validate(payload)
    _scan_replay_value(manifest)
    canonical = _canonical_bytes(manifest.model_dump(mode="json", exclude_none=True))
    if raw != canonical:
        raise HarnessFactoryReplayValidationError(
            "factory replay manifest is not canonical immutable JSON"
        )
    return manifest


def _write_execution_provenance(
    *,
    project_root: str | Path,
    provenance: HarnessFactoryReplayExecutionProvenance,
) -> Path:
    root = Path(project_root).expanduser().resolve()
    destination = root.joinpath(
        *REPLAY_EVIDENCE_DIR,
        provenance.replay_manifest_digest,
        f"{provenance.release_digest}.execution.json",
    )
    try:
        destination.resolve().relative_to(root)
    except ValueError as exc:
        raise HarnessFactoryReplayValidationError(
            "factory replay provenance path escapes the project"
        ) from exc
    raw = _canonical_bytes(provenance.model_dump(mode="json", exclude_none=True))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != raw:
            raise FileExistsError(
                "refusing to overwrite different factory replay provenance"
            )
        return destination
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    if destination.read_bytes() != raw:
        raise HarnessFactoryReplayValidationError(
            "factory replay provenance failed immutable write verification"
        )
    return destination


def build_harness_factory_release_from_replay(
    build_input: HarnessFactoryBuildInput,
    *,
    replay_manifest_path: str | Path,
) -> HarnessFactoryReplayBuildResult:
    """Preflight and publish F1 solely from an immutable deterministic transcript."""

    manifest = load_harness_factory_replay_manifest(replay_manifest_path)
    validate_harness_factory_replay_bindings(manifest, build_input=build_input)

    # Full reconciliation happens once before any factory release transaction.
    preflight = HarnessFactoryReplayCallbacks(manifest)
    preflight_result = run_paired_harness_search(
        epoch=build_input.epoch,
        tasks=build_input.task_panel,
        dependency_manifest=build_input.dependency_manifest,
        founding_protocol=build_input.founding_protocol,
        config=build_input.s1_config,
        proposal_callback=preflight.proposal,
        evaluator_callback=preflight.evaluator,
    )
    preflight.assert_reconciled()
    if (
        preflight_result.final_status.live_inference_status != "not_run"
        or preflight_result.final_status.inference_requests_sent != 0
    ):
        raise HarnessFactoryReplayValidationError(
            "deterministic replay preflight reported live inference"
        )

    callbacks = HarnessFactoryReplayCallbacks(manifest)
    service_result = build_harness_factory_release(
        build_input,
        proposal_callback=callbacks.proposal,
        evaluator_callback=callbacks.evaluator,
    )
    callbacks.assert_reconciled()
    if service_result.search_result_digest != preflight_result.result_digest:
        raise HarnessFactoryReplayValidationError(
            "factory service replay diverged from reconciled deterministic preflight"
        )
    if service_result.live_status != "not_run" or service_result.release_pointer is None:
        raise HarnessFactoryReplayValidationError(
            "factory replay did not produce a non-live immutable release"
        )
    pointer = service_result.release_pointer
    provenance = HarnessFactoryReplayExecutionProvenance(
        replay_manifest_id=manifest.manifest_id,
        replay_manifest_digest=manifest.manifest_digest,
        epoch_manifest_digest=manifest.epoch_manifest_digest,
        task_panel_digest=manifest.task_panel_digest,
        dependency_manifest_digest=manifest.dependency_manifest_digest,
        founding_protocol_digest=manifest.founding_protocol_digest,
        s1_config_digest=manifest.s1_config_digest,
        proposal_row_digests=tuple(row.row_digest for row in manifest.proposal_rows),
        evaluator_row_digests=tuple(row.row_digest for row in manifest.evaluator_rows),
        proposal_rows_consumed=callbacks.proposal.consumed,
        evaluator_rows_consumed=callbacks.evaluator.consumed,
        search_result_digest=service_result.search_result_digest,
        release_digest=pointer.release_digest,
        release_manifest_digest=pointer.manifest_digest,
        selected_protocol_digest=service_result.selected_protocol_digest,
    )
    provenance_path = _write_execution_provenance(
        project_root=build_input.project_root,
        provenance=provenance,
    )
    return HarnessFactoryReplayBuildResult(
        service_result=service_result,
        provenance=provenance,
        provenance_path=str(provenance_path),
    )


__all__ = [
    "CompiledTaskPlanIdentity",
    "EvaluatorReplayRow",
    "HARNESS_FACTORY_REPLAY_PROVENANCE_SCHEMA_VERSION",
    "HARNESS_FACTORY_REPLAY_SCHEMA_VERSION",
    "HarnessEvaluationRequestIdentity",
    "HarnessFactoryReplayBuildResult",
    "HarnessFactoryReplayCallbacks",
    "HarnessFactoryReplayError",
    "HarnessFactoryReplayExecutionProvenance",
    "HarnessFactoryReplayExhaustedError",
    "HarnessFactoryReplayIncompleteError",
    "HarnessFactoryReplayManifest",
    "HarnessFactoryReplayRecorder",
    "HarnessFactoryReplayValidationError",
    "ProposalBatchRequestIdentity",
    "ProposalReplayRow",
    "REPLAY_EVIDENCE_DIR",
    "ReplayEvaluatorCallback",
    "ReplayProposalCallback",
    "ReplayTaskIdentity",
    "build_harness_factory_release_from_replay",
    "build_harness_factory_replay_manifest",
    "harness_evaluation_request_digest",
    "harness_evaluation_request_identity",
    "load_harness_factory_replay_manifest",
    "proposal_batch_request_digest",
    "proposal_batch_request_identity",
    "validate_harness_factory_replay_bindings",
    "write_harness_factory_replay_manifest",
]
