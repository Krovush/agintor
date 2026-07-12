from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import (
    ResearchEpochManifest,
    TaskEnvelope,
    assert_task_bound_to_epoch,
)
from ..contracts.harness import (
    CompositeRunPlan,
    HarnessProtocol,
    RuntimeDependencyManifest,
)
from ..contracts.harness_actions import (
    SemanticTransaction,
    SemanticTransactionProposal,
)
from ..contracts.outcomes import (
    OutcomeReceipt,
    PairKey,
    pair_key_digest,
    pair_key_payload,
)
from ..contracts.promotion_proof import EvaluatorOutcomeProofBinding
from ..core.identity import evidence_digest
from ..evaluation.pairing import (
    JoinedOutcomePanel,
    PairingError,
    join_outcome_receipts,
)
from ..runtime.api.composite_compiler import compile_composite_run_plan
from .harness_mutator import (
    AppliedSemanticMutation,
    SemanticMutationError,
    apply_semantic_transaction,
)
from .promotion import (
    PromotionAuthorization,
    PromotionRefusal,
    assert_authoritative_outcome_proof,
    authorize_paired_search_retention,
)


PAIRED_HARNESS_SEARCH_SCHEMA_VERSION = "repo-repair-paired-harness-search-v1"
LIVE_SEARCH_AUTHORIZATION_SCHEMA_VERSION = "repo-repair-s1-live-authorization-v1"
REQUIRED_CONTROL_KINDS = (
    "equal_envelope_single_actor",
    "repeated_single_actor_fixed_selector",
    "static_localization_repair_validation",
    "founding_parent",
    "prompt_only",
    "matched_random_semantic",
)

ControlKind = Literal[
    "equal_envelope_single_actor",
    "repeated_single_actor_fixed_selector",
    "static_localization_repair_validation",
    "founding_parent",
    "prompt_only",
    "matched_random_semantic",
]


class PairedHarnessSearchError(ValueError):
    """The frozen S1 search cannot proceed safely."""


class PairedSearchIntegrityError(PairedHarnessSearchError):
    """Evaluator identities or exact PairKey coverage failed closed."""


class SearchConfigurationError(PairedHarnessSearchError):
    """The proposed search is not the frozen serial `(1+lambda)` design."""


class SearchRecordModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


def canonical_pair_keys(pair_keys: Sequence[PairKey]) -> tuple[PairKey, ...]:
    mapped: dict[str, PairKey] = {}
    for pair_key in pair_keys:
        digest = pair_key_digest(pair_key)
        if digest in mapped:
            raise SearchConfigurationError(f"duplicate expected PairKey {digest}")
        mapped[digest] = pair_key
    if not mapped:
        raise SearchConfigurationError("S1 requires a nonempty PairKey panel")
    return tuple(mapped[digest] for digest in sorted(mapped))


def paired_task_panel_digest(pair_keys: Sequence[PairKey]) -> str:
    canonical = canonical_pair_keys(pair_keys)
    return evidence_digest(
        {
            "kind": "repo-repair-s1-task-panel-v1",
            "pair_keys": [pair_key_payload(pair_key) for pair_key in canonical],
        }
    )


class LiveSearchAuthorization(SearchRecordModel):
    schema_version: Literal[LIVE_SEARCH_AUTHORIZATION_SCHEMA_VERSION] = (
        LIVE_SEARCH_AUTHORIZATION_SCHEMA_VERSION
    )
    authorization_id: str = Field(min_length=1)
    authorization_digest: str = ""
    search_id: str = Field(min_length=1)
    epoch_id: str = Field(min_length=1)
    epoch_manifest_digest: str
    deployment_profile_digest: str
    provider_config_digest: str
    authorized_by: str = Field(min_length=1)

    @field_validator(
        "epoch_manifest_digest",
        "deployment_profile_digest",
        "provider_config_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        normalized = str(value or "").strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def bind_authorization(self) -> "LiveSearchAuthorization":
        payload = self.model_dump(mode="python", exclude={"authorization_digest"})
        computed = evidence_digest(
            {"kind": LIVE_SEARCH_AUTHORIZATION_SCHEMA_VERSION, **payload}
        )
        if self.authorization_digest and self.authorization_digest != computed:
            raise ValueError("live search authorization digest mismatch")
        if not self.authorization_digest:
            object.__setattr__(self, "authorization_digest", computed)
        return self


class FrozenControlArm(SearchRecordModel):
    control_id: str = Field(min_length=1)
    control_kind: ControlKind
    protocol: HarnessProtocol
    origin_transaction: SemanticTransaction | None = None
    fixed_selector_id: str | None = None

    @model_validator(mode="after")
    def validate_control(self) -> "FrozenControlArm":
        if self.control_kind in {
            "equal_envelope_single_actor",
            "repeated_single_actor_fixed_selector",
        }:
            if len(self.protocol.actors) != 1:
                raise ValueError("single-actor controls must contain exactly one actor")
            if self.protocol.artifact_channels or self.protocol.revision is not None:
                raise ValueError("single-actor controls may not declare collaboration")
        if self.control_kind == "repeated_single_actor_fixed_selector":
            if not str(self.fixed_selector_id or "").strip():
                raise ValueError("repeated single-actor control requires a fixed selector")
        elif self.fixed_selector_id is not None:
            raise ValueError("fixed_selector_id belongs only to repeated single-actor control")
        if self.control_kind == "prompt_only":
            if self.origin_transaction is None:
                raise ValueError("prompt-only control requires its frozen transaction")
            if self.origin_transaction.treatment_class != "prompt_only_control":
                raise ValueError("prompt-only control transaction is not prompt-only")
        if self.control_kind == "matched_random_semantic":
            if self.origin_transaction is None:
                raise ValueError("matched-random control requires its frozen transaction")
            if self.origin_transaction.proposal_source != "matched_random":
                raise ValueError("matched-random control requires matched_random provenance")
            if self.origin_transaction.treatment_class != "structural":
                raise ValueError("matched-random control must be a structural transaction")
        if self.origin_transaction is not None:
            if (
                self.origin_transaction.child_source_protocol_digest
                != self.protocol.source_digest()
            ):
                raise ValueError("control transaction does not produce its protocol")
        return self


class PairedHarnessSearchConfig(SearchRecordModel):
    search_id: str = Field(min_length=1)
    execution_mode: Literal["offline_scripted", "live_provider", "dry_run"]
    expected_pair_keys: tuple[PairKey, ...] = Field(min_length=1)
    deployment_profile_digest: str
    controls: tuple[FrozenControlArm, ...]
    control_opportunities_per_arm: int = Field(gt=0)
    outcome_equivalence_complete_repairs: Literal[0] = 0
    racing_enabled: Literal[False] = False
    parallel_evaluation: Literal[False] = False
    live_authorization: LiveSearchAuthorization | None = None

    @field_validator("deployment_profile_digest")
    @classmethod
    def validate_deployment_profile_digest(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError(
                "deployment_profile_digest must be a lowercase SHA-256 digest"
            )
        return normalized

    @model_validator(mode="after")
    def validate_config(self) -> "PairedHarnessSearchConfig":
        canonical = canonical_pair_keys(self.expected_pair_keys)
        if self.expected_pair_keys != canonical:
            raise ValueError("expected_pair_keys must be unique and canonical")
        kinds = tuple(control.control_kind for control in self.controls)
        if tuple(sorted(kinds)) != tuple(sorted(REQUIRED_CONTROL_KINDS)):
            raise ValueError("S1 requires exactly one frozen arm of every control kind")
        control_ids = [control.control_id for control in self.controls]
        if len(control_ids) != len(set(control_ids)):
            raise ValueError("control_id values must be unique")
        if self.execution_mode == "live_provider":
            if self.live_authorization is None:
                raise ValueError(
                    "live_provider S1 requires an explicit live search authorization"
                )
            if self.live_authorization.search_id != self.search_id:
                raise ValueError("live search authorization crossed search_id")
            if (
                self.live_authorization.deployment_profile_digest
                != self.deployment_profile_digest
            ):
                raise ValueError("live search authorization crossed deployment profile")
        elif self.live_authorization is not None:
            raise ValueError("non-live S1 may not carry a live search authorization")
        return self


class CompiledTaskPlan(SearchRecordModel):
    task_manifest_id: str
    task_manifest_digest: str
    plan: CompositeRunPlan


@dataclass(frozen=True)
class HarnessEvaluationRequest:
    evaluation_id: str
    arm_id: str
    arm_kind: Literal["search_parent", "search_child", "control"]
    control_kind: ControlKind | None
    opportunity_index: int
    protocol: HarnessProtocol
    compiled_plans: tuple[CompiledTaskPlan, ...]
    expected_pair_keys: tuple[PairKey, ...]
    deployment_profile_digest: str
    execution_mode: Literal["offline_scripted", "live_provider"] = "offline_scripted"
    live_authorization_digest: str | None = None

    def __post_init__(self) -> None:
        if len(self.deployment_profile_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.deployment_profile_digest
        ):
            raise ValueError("evaluator request deployment profile digest is invalid")
        if (self.execution_mode == "live_provider") != bool(
            self.live_authorization_digest
        ):
            raise ValueError(
                "live evaluator requests require exactly one authorization digest"
            )


@dataclass(frozen=True)
class ProposalBatchRequest:
    search_id: str
    step_index: int
    requested_offspring: int
    remaining_candidate_budget: int
    incumbent_id: str
    incumbent_protocol: HarnessProtocol
    incumbent_anchor_plan: CompositeRunPlan
    anchor_task: TaskEnvelope
    dependency_manifest: RuntimeDependencyManifest
    retained_transactions: tuple[SemanticTransaction, ...]
    deployment_profile_digest: str
    execution_mode: Literal["offline_scripted", "live_provider"] = "offline_scripted"
    live_authorization_digest: str | None = None

    def __post_init__(self) -> None:
        if len(self.deployment_profile_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.deployment_profile_digest
        ):
            raise ValueError("proposal request deployment profile digest is invalid")
        if (self.execution_mode == "live_provider") != bool(
            self.live_authorization_digest
        ):
            raise ValueError(
                "live proposal requests require exactly one authorization digest"
            )


EvaluatorCallback = Callable[
    [HarnessEvaluationRequest],
    Sequence[EvaluatorOutcomeProofBinding],
]
ProposalCallback = Callable[[ProposalBatchRequest], Sequence[SemanticTransactionProposal]]


class PairOutcomeRecord(SearchRecordModel):
    pair_key: PairKey
    pair_key_digest: str
    parent_receipt_digest: str
    child_receipt_digest: str
    parent_complete_repair: bool
    child_complete_repair: bool
    child_known_and_estimated_cost_usd: float = Field(ge=0.0)
    child_wall_time_ms: int = Field(ge=0)


class CandidateLineageRecord(SearchRecordModel):
    candidate_id: str
    step_index: int = Field(ge=0)
    offspring_index: int = Field(ge=0)
    parent_candidate_id: str
    proposal: SemanticTransactionProposal
    transaction: SemanticTransaction | None = None
    protocol: HarnessProtocol | None = None
    compiled_plan_digests: dict[str, str] = Field(default_factory=dict)
    status: Literal[
        "invalid_transaction",
        "health_rejected",
        "outcome_rejected",
        "batch_not_selected",
        "promoted",
    ]
    reason: str
    child_proof_bindings: tuple[EvaluatorOutcomeProofBinding, ...] = ()
    joined_panel: JoinedOutcomePanel | None = None
    pair_outcomes: tuple[PairOutcomeRecord, ...] = ()
    promotion_authorization: PromotionAuthorization | None = None


class SelectionDecisionRecord(SearchRecordModel):
    step_index: int = Field(ge=0)
    incumbent_before_id: str
    candidate_ids: tuple[str, ...]
    authorized_candidate_ids: tuple[str, ...]
    selected_candidate_id: str
    selection_reason: str
    complete_repairs: int = Field(ge=0)
    total_known_and_estimated_cost_usd: float = Field(ge=0.0)
    total_wall_time_ms: int = Field(ge=0)
    protocol_simplicity: tuple[int, int, int, int]
    diagnostics_used_for_selection: Literal[False] = False


class ControlOpportunityRecord(SearchRecordModel):
    control_id: str
    control_kind: ControlKind
    opportunity_index: int = Field(ge=0)
    protocol_digest: str
    status: Literal["completed", "health_rejected", "not_run"]
    reason: str
    expected_pair_key_digests: tuple[str, ...]
    proof_bindings: tuple[EvaluatorOutcomeProofBinding, ...] = ()


class SearchFinalStatus(SearchRecordModel):
    execution_status: Literal[
        "dry_run",
        "completed",
        "feasibility_stop",
        "stopped_by_rule",
    ]
    feasibility_status: Literal[
        "not_run",
        "search_viable",
        "no_headroom_saturated",
        "no_headroom_uniform_failure",
        "no_valid_semantic_descendants",
        "no_outcome_improving_descendant",
    ]
    live_inference_status: Literal["not_run", "completed", "failed"] = "not_run"
    inference_requests_sent: int = Field(default=0, ge=0)
    stop_reason: str


class PairedHarnessSearchResult(SearchRecordModel):
    schema_version: Literal[PAIRED_HARNESS_SEARCH_SCHEMA_VERSION] = (
        PAIRED_HARNESS_SEARCH_SCHEMA_VERSION
    )
    search_id: str
    result_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    task_panel_digest: str
    execution_mode: Literal["offline_scripted", "live_provider", "dry_run"]
    founding_protocol: HarnessProtocol
    final_protocol: HarnessProtocol
    founding_candidate_id: str
    final_candidate_id: str
    founding_proof_bindings: tuple[EvaluatorOutcomeProofBinding, ...] = ()
    candidate_opportunities_used: int = Field(ge=0)
    evaluated_children: int = Field(ge=0)
    retained_children: int = Field(ge=0)
    candidate_lineage: tuple[CandidateLineageRecord, ...] = ()
    selection_decisions: tuple[SelectionDecisionRecord, ...] = ()
    control_opportunities: tuple[ControlOpportunityRecord, ...]
    retained_transactions: tuple[SemanticTransaction, ...] = ()
    capability_promotion_authorized: bool = False
    capability_promotion_reason: str = (
        "no retained paired child has capability promotion authority"
    )
    final_status: SearchFinalStatus

    @model_validator(mode="after")
    def bind_result(self) -> "PairedHarnessSearchResult":
        if self.execution_mode == "live_provider":
            if (
                self.final_status.live_inference_status == "not_run"
                or self.final_status.inference_requests_sent <= 0
            ):
                raise ValueError("live S1 result requires positive live inference provenance")
        elif (
            self.final_status.live_inference_status != "not_run"
            or self.final_status.inference_requests_sent != 0
        ):
            raise ValueError("non-live S1 result may not claim live execution provenance")

        final_authorization = next(
            (
                record.promotion_authorization
                for record in reversed(self.candidate_lineage)
                if record.status == "promoted"
                and record.candidate_id == self.final_candidate_id
                and record.promotion_authorization is not None
            ),
            None,
        )
        if final_authorization is None:
            expected_capability = False
            expected_reason = (
                "no retained paired child has capability promotion authority"
            )
        else:
            expected_capability = (
                final_authorization.capability_promotion_authorized
            )
            expected_reason = final_authorization.capability_promotion_reason
        if (
            self.capability_promotion_authorized != expected_capability
            or self.capability_promotion_reason != expected_reason
        ):
            raise ValueError("final capability claim crossed paired proof authorization")
        payload = self.model_dump(mode="python", exclude={"result_digest"})
        computed = evidence_digest(
            {"kind": PAIRED_HARNESS_SEARCH_SCHEMA_VERSION, **payload}
        )
        if self.result_digest and self.result_digest != computed:
            raise ValueError("paired search result_digest does not match its records")
        if not self.result_digest:
            object.__setattr__(self, "result_digest", computed)
        return self


@dataclass(frozen=True)
class _CandidateEvaluation:
    applied: AppliedSemanticMutation
    candidate_id: str
    plans: tuple[CompiledTaskPlan, ...]
    proof_bindings: tuple[EvaluatorOutcomeProofBinding, ...]
    joined_panel: JoinedOutcomePanel
    pair_outcomes: tuple[PairOutcomeRecord, ...]
    authorization: PromotionAuthorization
    complete_repairs: int
    total_cost: float
    total_wall_time_ms: int
    simplicity: tuple[int, int, int, int]


def _validate_search_inputs(
    *,
    epoch: ResearchEpochManifest,
    tasks: Sequence[TaskEnvelope],
    dependency_manifest: RuntimeDependencyManifest,
    config: PairedHarnessSearchConfig,
    founding_protocol: HarnessProtocol,
) -> tuple[tuple[TaskEnvelope, ...], dict[str, TaskEnvelope]]:
    if epoch.search_envelope.strategy != "one_plus_lambda":
        raise SearchConfigurationError("S1 supports only one_plus_lambda")
    if epoch.search_envelope.racing_enabled or epoch.search_envelope.parallel_candidates:
        raise SearchConfigurationError("racing and parallel candidate evaluation are disabled")
    if config.racing_enabled or config.parallel_evaluation:
        raise SearchConfigurationError("S1 config may not enable racing or parallelism")
    if paired_task_panel_digest(config.expected_pair_keys) != epoch.search_envelope.task_panel_digest:
        raise SearchConfigurationError("PairKey panel crossed the epoch task_panel_digest")
    authorization = config.live_authorization
    if config.execution_mode == "live_provider":
        if authorization is None:
            raise SearchConfigurationError("live S1 authorization is missing")
        crossed_authority = []
        if authorization.epoch_id != epoch.epoch_id:
            crossed_authority.append("epoch_id")
        if authorization.epoch_manifest_digest != epoch.epoch_manifest_digest:
            crossed_authority.append("epoch_manifest_digest")
        if (
            authorization.provider_config_digest
            != epoch.deployment.provider_config_digest
        ):
            crossed_authority.append("provider_config_digest")
        if crossed_authority:
            raise SearchConfigurationError(
                "live S1 authorization crossed epoch authority: "
                + ", ".join(crossed_authority)
            )
    if not tasks:
        raise SearchConfigurationError("S1 requires at least one development task")
    task_map: dict[str, TaskEnvelope] = {}
    for task in tasks:
        assert_task_bound_to_epoch(task, epoch)
        if task.data_state != "development":
            raise SearchConfigurationError("sealed-confirmation tasks may not enter S1")
        if task.task_manifest_id in task_map:
            raise SearchConfigurationError("task_manifest_id values must be unique")
        task_map[task.task_manifest_id] = task

    pair_task_ids = {pair_key.task_manifest_id for pair_key in config.expected_pair_keys}
    if pair_task_ids != set(task_map):
        raise SearchConfigurationError("PairKey panel does not exactly cover the task panel")
    for task_id in sorted(task_map):
        task_pairs = [
            pair_key
            for pair_key in config.expected_pair_keys
            if pair_key.task_manifest_id == task_id
        ]
        if len(task_pairs) != epoch.search_envelope.sampling_replicates:
            raise SearchConfigurationError(
                "every task must receive the frozen sampling replicate count"
            )
        if {pair.sampling_replicate for pair in task_pairs} != set(
            range(epoch.search_envelope.sampling_replicates)
        ):
            raise SearchConfigurationError("sampling replicates must be contiguous from zero")
        if any(
            pair.provider_config_digest != epoch.deployment.provider_config_digest
            for pair in task_pairs
        ):
            raise SearchConfigurationError("PairKey provider configuration crossed the epoch")

    if dependency_manifest.runtime_contract_version != epoch.runtime_contract_version:
        raise SearchConfigurationError("runtime dependency contract crossed the epoch")
    founding_digest = founding_protocol.source_digest()
    for control in config.controls:
        if control.control_kind == "founding_parent" and control.protocol.source_digest() != founding_digest:
            raise SearchConfigurationError("founding-parent control does not freeze the founding protocol")
    ordered_tasks = tuple(task_map[task_id] for task_id in sorted(task_map))
    return ordered_tasks, task_map


def _compile_protocol_panel(
    protocol: HarnessProtocol,
    tasks: Sequence[TaskEnvelope],
    dependencies: RuntimeDependencyManifest,
) -> tuple[CompiledTaskPlan, ...]:
    return tuple(
        CompiledTaskPlan(
            task_manifest_id=task.task_manifest_id,
            task_manifest_digest=task.task_manifest_digest,
            plan=compile_composite_run_plan(task, protocol, dependencies),
        )
        for task in tasks
    )


def _candidate_id(prefix: str, protocol: HarnessProtocol, nonce: Any) -> str:
    return prefix + "." + evidence_digest(
        {
            "protocol": protocol.source_digest(),
            "nonce": nonce,
        }
    )[:20]


def _evaluation_request(
    *,
    config: PairedHarnessSearchConfig,
    arm_id: str,
    arm_kind: Literal["search_parent", "search_child", "control"],
    control_kind: ControlKind | None,
    opportunity_index: int,
    protocol: HarnessProtocol,
    plans: tuple[CompiledTaskPlan, ...],
) -> HarnessEvaluationRequest:
    evaluation_id = "evaluation." + evidence_digest(
        {
            "search": config.search_id,
            "arm": arm_id,
            "kind": arm_kind,
            "opportunity": opportunity_index,
            "protocol": protocol.source_digest(),
        }
    )[:24]
    return HarnessEvaluationRequest(
        evaluation_id=evaluation_id,
        arm_id=arm_id,
        arm_kind=arm_kind,
        control_kind=control_kind,
        opportunity_index=opportunity_index,
        protocol=protocol,
        compiled_plans=plans,
        expected_pair_keys=config.expected_pair_keys,
        deployment_profile_digest=config.deployment_profile_digest,
        execution_mode=(
            "live_provider"
            if config.execution_mode == "live_provider"
            else "offline_scripted"
        ),
        live_authorization_digest=(
            config.live_authorization.authorization_digest
            if config.live_authorization is not None
            else None
        ),
    )


def _is_candidate_health_refusal(message: str) -> bool:
    markers = (
        "failed a process/no-leakage/integrity health floor",
        "reports an exceeded epoch envelope",
        "exceeds epoch ceilings",
        "unknown dollars",
        "excluded evaluator outcomes",
        "public RunEvidence failed its process integrity floor",
    )
    return any(marker in message for marker in markers)


def _validate_proof_panel(
    *,
    epoch: ResearchEpochManifest,
    task_map: Mapping[str, TaskEnvelope],
    expected_pair_keys: Sequence[PairKey],
    protocol: HarnessProtocol,
    dependencies: RuntimeDependencyManifest,
    compiled_plans: Sequence[CompiledTaskPlan],
    expected_profile_digest: str,
    execution_mode: Literal["offline_scripted", "live_provider"],
    proof_bindings: Sequence[EvaluatorOutcomeProofBinding],
) -> tuple[tuple[EvaluatorOutcomeProofBinding, ...], str | None]:
    expected = {pair_key_digest(key): key for key in expected_pair_keys}
    plans = {plan.task_manifest_id: plan for plan in compiled_plans}
    mapped: dict[str, EvaluatorOutcomeProofBinding] = {}
    health_reason: str | None = None
    for binding in proof_bindings:
        if not isinstance(binding, EvaluatorOutcomeProofBinding):
            raise PairedSearchIntegrityError(
                "evaluator callback returned a naked receipt instead of a proof binding"
            )
        receipt = binding.outcome_receipt
        digest = pair_key_digest(receipt.pair_key)
        if digest in mapped:
            raise PairedSearchIntegrityError(f"duplicate evaluator PairKey {digest}")
        mapped[digest] = binding
    missing = sorted(set(expected) - set(mapped))
    unexpected = sorted(set(mapped) - set(expected))
    if missing or unexpected:
        raise PairedSearchIntegrityError(
            f"evaluator PairKey coverage mismatch missing={missing!r} unexpected={unexpected!r}"
        )
    for digest in sorted(expected):
        binding = mapped[digest]
        receipt = binding.outcome_receipt
        run = binding.run_evidence
        pair_key = expected[digest]
        if receipt.pair_key != pair_key:
            raise PairedSearchIntegrityError("evaluator crossed a PairKey payload")
        task = task_map[pair_key.task_manifest_id]
        identity_errors = []
        if receipt.protocol_digest != protocol.source_digest():
            identity_errors.append("protocol_digest")
        if receipt.task_manifest_digest != task.task_manifest_digest:
            identity_errors.append("task_manifest_digest")
        if receipt.compiler_digest != dependencies.compiler.implementation_digest:
            identity_errors.append("compiler_digest")
        if receipt.kernel_digest != dependencies.kernel.implementation_digest:
            identity_errors.append("kernel_digest")
        if receipt.tool_manifest_digest != dependencies.manifest_digest():
            identity_errors.append("tool_manifest_digest")
        plan = plans.get(pair_key.task_manifest_id)
        if plan is None:
            identity_errors.append("compiled_task_plan")
        else:
            if run.compiled_semantic_digest != plan.plan.compiled_semantic_digest:
                identity_errors.append("compiled_semantic_digest")
            if run.dependency_manifest_digest != plan.plan.dependency_manifest_digest:
                identity_errors.append("dependency_manifest_digest")
        if identity_errors:
            raise PairedSearchIntegrityError(
                "evaluator receipt crossed candidate identities: "
                + ", ".join(identity_errors)
            )
        try:
            assert_authoritative_outcome_proof(
                binding,
                epoch,
                expected_profile_digest=expected_profile_digest,
            )
            if execution_mode == "live_provider" and (
                run.execution_mode != "live_provider"
                or run.live_inference_status != "completed"
                or run.real_inference_requests_sent <= 0
                or receipt.execution_mode != "live_provider"
                or receipt.live_inference_status != "completed"
                or receipt.real_inference_requests_sent <= 0
                or receipt.cost.model_calls
                != receipt.real_inference_requests_sent
            ):
                raise PromotionRefusal(
                    "live S1 requires completed live-provider proof with reconciled sent requests"
                )
        except PromotionRefusal as exc:
            message = str(exc)
            if _is_candidate_health_refusal(message):
                health_reason = message
            else:
                raise PairedSearchIntegrityError(message) from exc
    return tuple(mapped[digest] for digest in sorted(expected)), health_reason


def _evaluate(
    evaluator: EvaluatorCallback,
    request: HarnessEvaluationRequest,
    *,
    epoch: ResearchEpochManifest,
    task_map: Mapping[str, TaskEnvelope],
    dependencies: RuntimeDependencyManifest,
    expected_profile_digest: str,
) -> tuple[tuple[EvaluatorOutcomeProofBinding, ...], str | None]:
    try:
        proof_bindings = tuple(evaluator(request))
    except Exception as exc:
        raise PairedSearchIntegrityError(
            f"scripted evaluator failed for {request.arm_id!r}: {exc}"
        ) from exc
    return _validate_proof_panel(
        epoch=epoch,
        task_map=task_map,
        expected_pair_keys=request.expected_pair_keys,
        protocol=request.protocol,
        dependencies=dependencies,
        compiled_plans=request.compiled_plans,
        expected_profile_digest=expected_profile_digest,
        execution_mode=request.execution_mode,
        proof_bindings=proof_bindings,
    )


def _pair_records(panel: JoinedOutcomePanel) -> tuple[PairOutcomeRecord, ...]:
    return tuple(
        PairOutcomeRecord(
            pair_key=pair.pair_key,
            pair_key_digest=pair.pair_key_digest,
            parent_receipt_digest=pair.parent_receipt.receipt_digest,
            child_receipt_digest=pair.child_receipt.receipt_digest,
            parent_complete_repair=pair.parent_receipt.complete_repair,
            child_complete_repair=pair.child_receipt.complete_repair,
            child_known_and_estimated_cost_usd=(
                pair.child_receipt.cost.known_cost_usd
                + pair.child_receipt.cost.estimated_cost_usd
            ),
            child_wall_time_ms=pair.child_receipt.cost.wall_time_ms,
        )
        for pair in panel.pairs
    )


def _search_execution_provenance(
    execution_mode: str,
    proof_bindings: Sequence[EvaluatorOutcomeProofBinding],
) -> tuple[Literal["not_run", "completed"], int]:
    if execution_mode != "live_provider":
        return "not_run", 0
    digests = [binding.binding_digest for binding in proof_bindings]
    if len(digests) != len(set(digests)):
        raise PairedSearchIntegrityError(
            "live evaluator proof binding was reused across search opportunities"
        )
    requests_sent = sum(
        binding.run_evidence.real_inference_requests_sent
        for binding in proof_bindings
    )
    if requests_sent <= 0:
        raise PairedSearchIntegrityError(
            "live S1 completed without positive sent-request provenance"
        )
    return "completed", requests_sent


def _simplicity(protocol: HarnessProtocol, plans: Sequence[CompiledTaskPlan]) -> tuple[int, int, int, int]:
    max_calls = max(len(item.plan.actor_calls) for item in plans)
    return (
        max_calls,
        len(protocol.actors),
        len(protocol.artifact_channels),
        sum(len(actor.instruction) for actor in protocol.actors)
        + (len(protocol.revision.instruction) if protocol.revision else 0),
    )


def _dry_run_result(
    *,
    epoch: ResearchEpochManifest,
    config: PairedHarnessSearchConfig,
    founding_protocol: HarnessProtocol,
) -> PairedHarnessSearchResult:
    pair_digests = tuple(pair_key_digest(key) for key in config.expected_pair_keys)
    control_records = tuple(
        ControlOpportunityRecord(
            control_id=control.control_id,
            control_kind=control.control_kind,
            opportunity_index=index,
            protocol_digest=control.protocol.source_digest(),
            status="not_run",
            reason="dry-run manifest; no evaluator request sent",
            expected_pair_key_digests=pair_digests,
        )
        for control in config.controls
        for index in range(config.control_opportunities_per_arm)
    )
    founding_id = _candidate_id("candidate.founding", founding_protocol, config.search_id)
    return PairedHarnessSearchResult(
        search_id=config.search_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        task_panel_digest=epoch.search_envelope.task_panel_digest,
        execution_mode=config.execution_mode,
        founding_protocol=founding_protocol,
        final_protocol=founding_protocol,
        founding_candidate_id=founding_id,
        final_candidate_id=founding_id,
        candidate_opportunities_used=0,
        evaluated_children=0,
        retained_children=0,
        control_opportunities=control_records,
        final_status=SearchFinalStatus(
            execution_status="dry_run",
            feasibility_status="not_run",
            stop_reason="validated dry-run; live and scripted evaluation not run",
        ),
    )


def run_paired_harness_search(
    *,
    epoch: ResearchEpochManifest,
    tasks: Sequence[TaskEnvelope],
    dependency_manifest: RuntimeDependencyManifest,
    founding_protocol: HarnessProtocol,
    config: PairedHarnessSearchConfig,
    proposal_callback: ProposalCallback | None = None,
    evaluator_callback: EvaluatorCallback | None = None,
) -> PairedHarnessSearchResult:
    ordered_tasks, task_map = _validate_search_inputs(
        epoch=epoch,
        tasks=tasks,
        dependency_manifest=dependency_manifest,
        config=config,
        founding_protocol=founding_protocol,
    )
    founding_plans = _compile_protocol_panel(
        founding_protocol,
        ordered_tasks,
        dependency_manifest,
    )
    for control in config.controls:
        _compile_protocol_panel(control.protocol, ordered_tasks, dependency_manifest)
    if config.execution_mode == "dry_run":
        if proposal_callback is not None or evaluator_callback is not None:
            raise SearchConfigurationError("dry_run may not receive execution callbacks")
        return _dry_run_result(
            epoch=epoch,
            config=config,
            founding_protocol=founding_protocol,
        )
    if proposal_callback is None or evaluator_callback is None:
        raise SearchConfigurationError(
            "offline_scripted search requires proposal and evaluator callbacks"
        )

    founding_id = _candidate_id("candidate.founding", founding_protocol, config.search_id)
    founding_request = _evaluation_request(
        config=config,
        arm_id=founding_id,
        arm_kind="search_parent",
        control_kind=None,
        opportunity_index=0,
        protocol=founding_protocol,
        plans=founding_plans,
    )
    founding_proofs, founding_health = _evaluate(
        evaluator_callback,
        founding_request,
        epoch=epoch,
        task_map=task_map,
        dependencies=dependency_manifest,
        expected_profile_digest=config.deployment_profile_digest,
    )
    if founding_health is not None:
        raise PairedSearchIntegrityError(
            "founding parent failed the search health floor: " + founding_health
        )

    founding_receipts = tuple(
        binding.outcome_receipt for binding in founding_proofs
    )
    complete_count = sum(receipt.complete_repair for receipt in founding_receipts)
    panel_size = len(founding_receipts)
    pair_digests = tuple(pair_key_digest(key) for key in config.expected_pair_keys)
    if complete_count in {0, panel_size}:
        live_status, live_requests = _search_execution_provenance(
            config.execution_mode,
            founding_proofs,
        )
        feasibility = (
            "no_headroom_saturated" if complete_count == panel_size else "no_headroom_uniform_failure"
        )
        return PairedHarnessSearchResult(
            search_id=config.search_id,
            epoch_id=epoch.epoch_id,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            task_panel_digest=epoch.search_envelope.task_panel_digest,
            execution_mode=config.execution_mode,
            founding_protocol=founding_protocol,
            final_protocol=founding_protocol,
            founding_candidate_id=founding_id,
            final_candidate_id=founding_id,
            founding_proof_bindings=founding_proofs,
            candidate_opportunities_used=0,
            evaluated_children=0,
            retained_children=0,
            control_opportunities=tuple(
                ControlOpportunityRecord(
                    control_id=control.control_id,
                    control_kind=control.control_kind,
                    opportunity_index=index,
                    protocol_digest=control.protocol.source_digest(),
                    status="not_run",
                    reason="feasibility stopped before search controls",
                    expected_pair_key_digests=pair_digests,
                )
                for control in config.controls
                for index in range(config.control_opportunities_per_arm)
            ),
            final_status=SearchFinalStatus(
                execution_status="feasibility_stop",
                feasibility_status=feasibility,
                live_inference_status=live_status,
                inference_requests_sent=live_requests,
                stop_reason="founding baseline lacks measured outcome headroom",
            ),
        )

    control_records: list[ControlOpportunityRecord] = []
    for control in config.controls:
        plans = _compile_protocol_panel(control.protocol, ordered_tasks, dependency_manifest)
        for opportunity_index in range(config.control_opportunities_per_arm):
            request = _evaluation_request(
                config=config,
                arm_id=control.control_id,
                arm_kind="control",
                control_kind=control.control_kind,
                opportunity_index=opportunity_index,
                protocol=control.protocol,
                plans=plans,
            )
            proof_bindings, health_reason = _evaluate(
                evaluator_callback,
                request,
                epoch=epoch,
                task_map=task_map,
                dependencies=dependency_manifest,
                expected_profile_digest=config.deployment_profile_digest,
            )
            control_records.append(
                ControlOpportunityRecord(
                    control_id=control.control_id,
                    control_kind=control.control_kind,
                    opportunity_index=opportunity_index,
                    protocol_digest=control.protocol.source_digest(),
                    status="health_rejected" if health_reason else "completed",
                    reason=health_reason or "completed through the shared evaluator boundary",
                    expected_pair_key_digests=pair_digests,
                    proof_bindings=proof_bindings,
                )
            )

    incumbent_id = founding_id
    incumbent_protocol = founding_protocol
    incumbent_plans = founding_plans
    incumbent_proofs = founding_proofs
    incumbent_receipts = founding_receipts
    retained_transactions: list[SemanticTransaction] = []
    lineage: list[CandidateLineageRecord] = []
    decisions: list[SelectionDecisionRecord] = []
    candidate_opportunities = 0
    evaluated_children = 0
    retained_children = 0
    valid_children = 0
    consecutive_nonimproving = 0
    stop_reason = "search envelope exhausted"
    stopped_by_rule = False

    max_opportunities = min(
        epoch.stop_rule.max_candidate_evaluations,
        epoch.search_envelope.max_steps * epoch.search_envelope.offspring_per_step,
    )
    anchor_task = ordered_tasks[0]
    for step_index in range(epoch.search_envelope.max_steps):
        if candidate_opportunities >= max_opportunities:
            stop_reason = "frozen candidate budget exhausted"
            break
        if consecutive_nonimproving >= epoch.stop_rule.max_consecutive_non_improving_steps:
            stop_reason = "frozen consecutive non-improvement stop rule reached"
            stopped_by_rule = True
            break
        remaining = max_opportunities - candidate_opportunities
        requested = min(epoch.search_envelope.offspring_per_step, remaining)
        anchor_plan = next(
            item.plan
            for item in incumbent_plans
            if item.task_manifest_id == anchor_task.task_manifest_id
        )
        batch_request = ProposalBatchRequest(
            search_id=config.search_id,
            step_index=step_index,
            requested_offspring=requested,
            remaining_candidate_budget=remaining,
            incumbent_id=incumbent_id,
            incumbent_protocol=incumbent_protocol,
            incumbent_anchor_plan=anchor_plan,
            anchor_task=anchor_task,
            dependency_manifest=dependency_manifest,
            retained_transactions=tuple(retained_transactions),
            deployment_profile_digest=config.deployment_profile_digest,
            execution_mode=(
                "live_provider"
                if config.execution_mode == "live_provider"
                else "offline_scripted"
            ),
            live_authorization_digest=(
                config.live_authorization.authorization_digest
                if config.live_authorization is not None
                else None
            ),
        )
        proposals = tuple(proposal_callback(batch_request))
        if len(proposals) != requested:
            raise SearchConfigurationError(
                "proposal callback must return the exact frozen offspring count"
            )

        batch_records: list[CandidateLineageRecord] = []
        eligible: list[_CandidateEvaluation] = []
        for offspring_index, proposal in enumerate(proposals):
            candidate_opportunities += 1
            try:
                applied = apply_semantic_transaction(
                    incumbent_protocol,
                    anchor_plan,
                    anchor_task,
                    dependency_manifest,
                    proposal,
                    retained_transactions=tuple(retained_transactions),
                )
            except SemanticMutationError as exc:
                batch_records.append(
                    CandidateLineageRecord(
                        candidate_id=f"candidate.invalid.{proposal.transaction_id}",
                        step_index=step_index,
                        offspring_index=offspring_index,
                        parent_candidate_id=incumbent_id,
                        proposal=proposal,
                        status="invalid_transaction",
                        reason=str(exc),
                    )
                )
                continue

            valid_children += 1
            candidate_id = _candidate_id(
                "candidate.child",
                applied.child_protocol,
                applied.transaction.transaction_record_digest,
            )
            child_plans = _compile_protocol_panel(
                applied.child_protocol,
                ordered_tasks,
                dependency_manifest,
            )
            request = _evaluation_request(
                config=config,
                arm_id=candidate_id,
                arm_kind="search_child",
                control_kind=None,
                opportunity_index=0,
                protocol=applied.child_protocol,
                plans=child_plans,
            )
            child_proofs, health_reason = _evaluate(
                evaluator_callback,
                request,
                epoch=epoch,
                task_map=task_map,
                dependencies=dependency_manifest,
                expected_profile_digest=config.deployment_profile_digest,
            )
            child_receipts = tuple(
                binding.outcome_receipt for binding in child_proofs
            )
            evaluated_children += 1
            compiled_digests = {
                item.task_manifest_id: item.plan.compiled_semantic_digest
                for item in child_plans
            }
            if health_reason is not None:
                batch_records.append(
                    CandidateLineageRecord(
                        candidate_id=candidate_id,
                        step_index=step_index,
                        offspring_index=offspring_index,
                        parent_candidate_id=incumbent_id,
                        proposal=proposal,
                        transaction=applied.transaction,
                        protocol=applied.child_protocol,
                        compiled_plan_digests=compiled_digests,
                        status="health_rejected",
                        reason=health_reason,
                        child_proof_bindings=child_proofs,
                    )
                )
                continue
            try:
                joined = join_outcome_receipts(
                    epoch=epoch,
                    expected_pair_keys=config.expected_pair_keys,
                    parent_receipts=incumbent_receipts,
                    child_receipts=child_receipts,
                )
            except PairingError as exc:
                raise PairedSearchIntegrityError(str(exc)) from exc
            pair_records = _pair_records(joined)
            try:
                authorization = authorize_paired_search_retention(
                    epoch=epoch,
                    parent_proofs=incumbent_proofs,
                    child_proofs=child_proofs,
                    expected_profile_digest=config.deployment_profile_digest,
                )
            except PromotionRefusal as exc:
                batch_records.append(
                    CandidateLineageRecord(
                        candidate_id=candidate_id,
                        step_index=step_index,
                        offspring_index=offspring_index,
                        parent_candidate_id=incumbent_id,
                        proposal=proposal,
                        transaction=applied.transaction,
                        protocol=applied.child_protocol,
                        compiled_plan_digests=compiled_digests,
                        status="outcome_rejected",
                        reason=str(exc),
                        child_proof_bindings=child_proofs,
                        joined_panel=joined,
                        pair_outcomes=pair_records,
                    )
                )
                continue
            complete_repairs = sum(receipt.complete_repair for receipt in child_receipts)
            total_cost = sum(
                receipt.cost.known_cost_usd + receipt.cost.estimated_cost_usd
                for receipt in child_receipts
            )
            total_wall = sum(receipt.cost.wall_time_ms for receipt in child_receipts)
            eligible.append(
                _CandidateEvaluation(
                    applied=applied,
                    candidate_id=candidate_id,
                    plans=child_plans,
                    proof_bindings=child_proofs,
                    joined_panel=joined,
                    pair_outcomes=pair_records,
                    authorization=authorization,
                    complete_repairs=complete_repairs,
                    total_cost=total_cost,
                    total_wall_time_ms=total_wall,
                    simplicity=_simplicity(applied.child_protocol, child_plans),
                )
            )

        selected: _CandidateEvaluation | None = None
        if eligible:
            selected = min(
                eligible,
                key=lambda item: (
                    -item.complete_repairs,
                    item.total_cost,
                    item.total_wall_time_ms,
                    item.simplicity,
                    item.candidate_id,
                ),
            )
            for item in eligible:
                batch_records.append(
                    CandidateLineageRecord(
                        candidate_id=item.candidate_id,
                        step_index=step_index,
                        offspring_index=next(
                            index
                            for index, proposal in enumerate(proposals)
                            if proposal.transaction_id
                            == item.applied.transaction.transaction_id
                        ),
                        parent_candidate_id=incumbent_id,
                        proposal=next(
                            proposal
                            for proposal in proposals
                            if proposal.transaction_id
                            == item.applied.transaction.transaction_id
                        ),
                        transaction=item.applied.transaction,
                        protocol=item.applied.child_protocol,
                        compiled_plan_digests={
                            plan.task_manifest_id: plan.plan.compiled_semantic_digest
                            for plan in item.plans
                        },
                        status=(
                            "promoted"
                            if item.candidate_id == selected.candidate_id
                            else "batch_not_selected"
                        ),
                        reason=(
                            "selected lexicographically from A0b-authorized outcomes"
                            if item.candidate_id == selected.candidate_id
                            else "authorized child lost the frozen outcome/cost/simplicity tie-break"
                        ),
                        child_proof_bindings=item.proof_bindings,
                        joined_panel=item.joined_panel,
                        pair_outcomes=item.pair_outcomes,
                        promotion_authorization=item.authorization,
                    )
                )

        lineage.extend(batch_records)
        before_id = incumbent_id
        if selected is None:
            consecutive_nonimproving += 1
            decisions.append(
                SelectionDecisionRecord(
                    step_index=step_index,
                    incumbent_before_id=before_id,
                    candidate_ids=tuple(record.candidate_id for record in batch_records),
                    authorized_candidate_ids=(),
                    selected_candidate_id=before_id,
                    selection_reason="no child passed exact pairing and A0b outcome authorization",
                    complete_repairs=sum(
                        receipt.complete_repair for receipt in incumbent_receipts
                    ),
                    total_known_and_estimated_cost_usd=sum(
                        receipt.cost.known_cost_usd + receipt.cost.estimated_cost_usd
                        for receipt in incumbent_receipts
                    ),
                    total_wall_time_ms=sum(
                        receipt.cost.wall_time_ms for receipt in incumbent_receipts
                    ),
                    protocol_simplicity=_simplicity(incumbent_protocol, incumbent_plans),
                )
            )
        else:
            incumbent_id = selected.candidate_id
            incumbent_protocol = selected.applied.child_protocol
            incumbent_plans = selected.plans
            incumbent_proofs = selected.proof_bindings
            incumbent_receipts = tuple(
                binding.outcome_receipt for binding in incumbent_proofs
            )
            retained_transactions.append(selected.applied.transaction)
            retained_children += 1
            consecutive_nonimproving = 0
            decisions.append(
                SelectionDecisionRecord(
                    step_index=step_index,
                    incumbent_before_id=before_id,
                    candidate_ids=tuple(record.candidate_id for record in batch_records),
                    authorized_candidate_ids=tuple(
                        sorted(item.candidate_id for item in eligible)
                    ),
                    selected_candidate_id=selected.candidate_id,
                    selection_reason="complete repair outcomes, then cost/latency, then simplicity",
                    complete_repairs=selected.complete_repairs,
                    total_known_and_estimated_cost_usd=selected.total_cost,
                    total_wall_time_ms=selected.total_wall_time_ms,
                    protocol_simplicity=selected.simplicity,
                )
            )

    if retained_children:
        feasibility_status = "search_viable"
        execution_status = "stopped_by_rule" if stopped_by_rule else "completed"
    elif valid_children == 0:
        feasibility_status = "no_valid_semantic_descendants"
        execution_status = "feasibility_stop"
        stop_reason = "no valid semantic descendants were produced"
    else:
        feasibility_status = "no_outcome_improving_descendant"
        execution_status = "feasibility_stop"
        stop_reason = "no A0b-authorized outcome-improving descendant was retained"

    all_proofs = (
        *founding_proofs,
        *(
            binding
            for record in control_records
            for binding in record.proof_bindings
        ),
        *(
            binding
            for record in lineage
            for binding in record.child_proof_bindings
        ),
    )
    live_status, live_requests = _search_execution_provenance(
        config.execution_mode,
        all_proofs,
    )
    final_authorization = next(
        (
            record.promotion_authorization
            for record in reversed(lineage)
            if record.status == "promoted"
            and record.candidate_id == incumbent_id
            and record.promotion_authorization is not None
        ),
        None,
    )
    capability_authorized = bool(
        final_authorization
        and final_authorization.capability_promotion_authorized
    )
    capability_reason = (
        final_authorization.capability_promotion_reason
        if final_authorization is not None
        else "no retained paired child has capability promotion authority"
    )

    return PairedHarnessSearchResult(
        search_id=config.search_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        task_panel_digest=epoch.search_envelope.task_panel_digest,
        execution_mode=config.execution_mode,
        founding_protocol=founding_protocol,
        final_protocol=incumbent_protocol,
        founding_candidate_id=founding_id,
        final_candidate_id=incumbent_id,
        founding_proof_bindings=founding_proofs,
        candidate_opportunities_used=candidate_opportunities,
        evaluated_children=evaluated_children,
        retained_children=retained_children,
        candidate_lineage=tuple(lineage),
        selection_decisions=tuple(decisions),
        control_opportunities=tuple(control_records),
        retained_transactions=tuple(retained_transactions),
        capability_promotion_authorized=capability_authorized,
        capability_promotion_reason=capability_reason,
        final_status=SearchFinalStatus(
            execution_status=execution_status,
            feasibility_status=feasibility_status,
            live_inference_status=live_status,
            inference_requests_sent=live_requests,
            stop_reason=stop_reason,
        ),
    )


def write_paired_harness_search_result(
    path: str | Path,
    result: PairedHarnessSearchResult,
) -> Path:
    """Persist one immutable, digest-bound S1 search ledger atomically."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = load_paired_harness_search_result(destination)
        if existing.result_digest != result.result_digest:
            raise PairedHarnessSearchError(
                "refusing to overwrite a different paired-search result"
            )
        return destination
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def load_paired_harness_search_result(
    path: str | Path,
) -> PairedHarnessSearchResult:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return PairedHarnessSearchResult.model_validate(payload)


__all__ = [
    "CandidateLineageRecord",
    "ControlOpportunityRecord",
    "FrozenControlArm",
    "HarnessEvaluationRequest",
    "LIVE_SEARCH_AUTHORIZATION_SCHEMA_VERSION",
    "LiveSearchAuthorization",
    "PairedHarnessSearchConfig",
    "PairedHarnessSearchError",
    "PairedHarnessSearchResult",
    "PairedSearchIntegrityError",
    "ProposalBatchRequest",
    "SearchConfigurationError",
    "SearchFinalStatus",
    "SelectionDecisionRecord",
    "canonical_pair_keys",
    "paired_task_panel_digest",
    "load_paired_harness_search_result",
    "run_paired_harness_search",
    "write_paired_harness_search_result",
]
