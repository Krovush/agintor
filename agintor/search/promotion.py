from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts.epochs import ResearchEpochManifest
from ..contracts.outcomes import (
    OutcomeReceipt,
    outcome_receipt_digest,
    pair_key_digest,
)
from ..contracts.promotion_proof import (
    EvaluatorOutcomeProofBinding,
    evaluator_outcome_proof_binding_digest,
    public_promotion_proof_digest,
)
from ..core.identity import evidence_digest


class PromotionRefusal(ValueError):
    """Raised when a candidate lacks evaluator-owned paired proof authority."""


class PromotionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    authorization_id: str
    authorization_digest: str = ""
    decision: Literal["retain_child"] = "retain_child"
    epoch_id: str
    epoch_manifest_digest: str
    parent_protocol_digest: str
    child_protocol_digest: str
    pair_key_digests: tuple[str, ...]
    parent_receipt_digests: tuple[str, ...]
    child_receipt_digests: tuple[str, ...]
    parent_proof_binding_digests: tuple[str, ...]
    child_proof_binding_digests: tuple[str, ...]
    complete_repair_gain: int = Field(ge=1)
    pair_regressions: int = Field(ge=0)
    capability_promotion_authorized: bool
    capability_promotion_reason: str

    @model_validator(mode="after")
    def validate_authorization_digest(self) -> "PromotionAuthorization":
        if self.capability_promotion_authorized:
            expected_reason = "paired improvement is backed by live-provider RunEvidence"
        else:
            expected_reason = (
                "paired improvement is implementation evidence only; "
                "live-provider RunEvidence is required for capability promotion"
            )
        if self.capability_promotion_reason != expected_reason:
            raise ValueError("capability promotion reason does not match proof provenance")
        payload = self.model_dump(mode="python", exclude={"authorization_digest"})
        computed = evidence_digest({"kind": "repo-repair-promotion-v2", **payload})
        if self.authorization_digest and self.authorization_digest != computed:
            raise ValueError("authorization_digest does not match promotion authorization")
        if not self.authorization_digest:
            object.__setattr__(self, "authorization_digest", computed)
        return self


def _refuse(message: str) -> None:
    raise PromotionRefusal(message)


def _assert_cost_within_epoch(
    receipt: OutcomeReceipt,
    epoch: ResearchEpochManifest,
) -> None:
    cost = receipt.cost
    ceilings = epoch.per_run_ceilings
    if cost.unknown_dollars:
        _refuse("outcome cost contains unknown dollars")
    if not cost.within_epoch_envelope:
        _refuse("outcome receipt reports an exceeded epoch envelope")
    checks = {
        "model calls": (cost.model_calls, ceilings.max_model_calls),
        "input tokens": (cost.input_tokens, ceilings.max_input_tokens),
        "output tokens": (cost.output_tokens, ceilings.max_output_tokens),
        "cached tokens": (cost.cached_tokens, ceilings.max_cached_tokens),
        "cache-write tokens": (cost.cache_write_tokens, ceilings.max_cache_write_tokens),
        "tool calls": (cost.tool_calls, ceilings.max_tool_calls),
        "tool output bytes": (cost.tool_output_bytes, ceilings.max_tool_output_bytes),
        "artifact bytes": (cost.artifact_bytes, ceilings.max_artifact_bytes),
        "patch bytes": (cost.patch_bytes, ceilings.max_patch_bytes),
        "retries": (cost.retries, ceilings.max_retries),
        "wall time": (cost.wall_time_ms, ceilings.max_wall_time_ms),
        "known cost": (cost.known_cost_usd, ceilings.max_known_cost_usd),
        "estimated cost": (
            cost.known_cost_usd + cost.estimated_cost_usd,
            ceilings.max_estimated_cost_usd,
        ),
    }
    exceeded = [name for name, (value, ceiling) in checks.items() if value > ceiling]
    if exceeded:
        _refuse("outcome receipt exceeds epoch ceilings: " + ", ".join(exceeded))


def assert_authoritative_outcome_receipt(
    receipt: OutcomeReceipt,
    epoch: ResearchEpochManifest,
) -> None:
    """Validate an evaluator receipt for integrity contexts, not promotion.

    Capability retention and promotion must additionally use
    :func:`assert_authoritative_outcome_proof`.
    """

    if not isinstance(receipt, OutcomeReceipt):
        _refuse("outcome integrity validation requires an OutcomeReceipt")
    if receipt.receipt_digest != outcome_receipt_digest(receipt):
        _refuse("outcome receipt digest does not match its payload")
    if receipt.runtime_contract_version != epoch.runtime_contract_version:
        _refuse("outcome receipt runtime contract version crossed the epoch")
    if receipt.capability_epoch != epoch.capability_epoch:
        _refuse("outcome receipt capability crossed the epoch")
    if receipt.epoch_id != epoch.epoch_id:
        _refuse("outcome receipt epoch_id crossed the pinned epoch")
    if receipt.epoch_manifest_digest != epoch.epoch_manifest_digest:
        _refuse("outcome receipt epoch digest crossed the pinned epoch")
    if receipt.data_state != "development":
        _refuse("sealed-confirmation outcomes may not drive development decisions")
    if receipt.split_manifest_digest != epoch.development_split_digest:
        _refuse("outcome receipt crossed the pinned development split")
    authority = epoch.evaluator_authority
    if receipt.evaluator_id != authority.evaluator_id:
        _refuse("outcome receipt evaluator_id crossed the pinned evaluator")
    if receipt.evaluator_identity_digest != authority.evaluator_identity_digest:
        _refuse("outcome receipt evaluator digest crossed the pinned evaluator")
    if receipt.evaluation_policy_digest != authority.evaluation_policy_digest:
        _refuse("outcome receipt evaluation policy crossed the pinned evaluator")
    if receipt.pair_key.provider_config_digest != epoch.deployment.provider_config_digest:
        _refuse("outcome receipt provider configuration crossed the pinned deployment")
    deployment_receipt_checks = {
        "provider_config_digest": epoch.deployment.provider_config_digest,
        "decoding_policy_digest": epoch.deployment.decoding_policy_digest,
        "price_schedule_digest": epoch.deployment.price_schedule_digest,
        "command_container_policy_digest": (
            epoch.deployment.command_container_policy_digest
        ),
    }
    crossed = [
        field_name
        for field_name, expected in deployment_receipt_checks.items()
        if getattr(receipt, field_name) != expected
    ]
    if crossed:
        _refuse(
            "outcome receipt crossed the pinned deployment: " + ", ".join(crossed)
        )
    if not receipt.health.passes_promotion_floor:
        _refuse("outcome receipt failed a process/no-leakage/integrity health floor")
    if receipt.exclusions:
        _refuse("excluded evaluator outcomes may not authorize development decisions")
    _assert_cost_within_epoch(receipt, epoch)


def assert_authoritative_outcome_proof(
    binding: EvaluatorOutcomeProofBinding,
    epoch: ResearchEpochManifest,
    *,
    expected_profile_digest: str | None = None,
) -> None:
    """Validate proof links, authority, deployment, and health floors."""

    if not isinstance(binding, EvaluatorOutcomeProofBinding):
        _refuse("capability decisions require an evaluator outcome proof binding")
    if binding.public_proof_digest != public_promotion_proof_digest(binding):
        _refuse("public proof digest does not match its projection")
    if binding.binding_digest != evaluator_outcome_proof_binding_digest(binding):
        _refuse("evaluator outcome proof binding digest does not match its payload")
    receipt = binding.outcome_receipt
    run = binding.run_evidence
    assert_authoritative_outcome_receipt(receipt, epoch)

    deployment_checks = {
        "deployment_id": epoch.deployment.deployment_id,
        "provider": epoch.deployment.provider,
        "model": epoch.deployment.model,
        "provider_config_digest": epoch.deployment.provider_config_digest,
        "decoding_policy_digest": epoch.deployment.decoding_policy_digest,
        "price_schedule_digest": epoch.deployment.price_schedule_digest,
        "command_container_policy_digest": (
            epoch.deployment.command_container_policy_digest
        ),
    }
    crossed_deployment = [
        field_name
        for field_name, expected_value in deployment_checks.items()
        if getattr(run, field_name) != expected_value
    ]
    if crossed_deployment:
        _refuse(
            "public RunEvidence crossed the pinned deployment: "
            + ", ".join(crossed_deployment)
        )
    if expected_profile_digest is not None and run.profile_digest != expected_profile_digest:
        _refuse("public RunEvidence crossed the frozen deployment profile")
    if run.arm != "intact" or run.intervention_digest is not None:
        _refuse("intervention RunEvidence may not authorize search retention")
    if not run.healthy:
        _refuse("public RunEvidence failed its process integrity floor")


def _proof_map(
    bindings: Sequence[EvaluatorOutcomeProofBinding],
    *,
    label: str,
    epoch: ResearchEpochManifest,
    expected_profile_digest: str | None,
) -> dict[str, EvaluatorOutcomeProofBinding]:
    if not bindings:
        _refuse(f"{label} evaluator outcome proof bindings are required")
    mapped: dict[str, EvaluatorOutcomeProofBinding] = {}
    for binding in bindings:
        assert_authoritative_outcome_proof(
            binding,
            epoch,
            expected_profile_digest=expected_profile_digest,
        )
        receipt = binding.outcome_receipt
        key = pair_key_digest(receipt.pair_key)
        if key in mapped:
            _refuse(f"duplicate {label} PairKey is not promotion evidence")
        mapped[key] = binding
    release_identities = {
        (
            binding.run_evidence.release_digest,
            binding.run_evidence.release_manifest_digest,
            binding.run_evidence.profile_digest,
            binding.run_evidence.deployment_id,
            binding.run_evidence.provider,
            binding.run_evidence.model,
            binding.run_evidence.provider_config_digest,
            binding.run_evidence.decoding_policy_digest,
            binding.run_evidence.price_schedule_digest,
            binding.run_evidence.command_container_policy_digest,
        )
        for binding in mapped.values()
    }
    if len(release_identities) != 1:
        _refuse(f"{label} proof panel crossed release or deployment identities")
    return mapped


def _live_capability_proof(binding: EvaluatorOutcomeProofBinding) -> bool:
    run = binding.run_evidence
    receipt = binding.outcome_receipt
    return (
        getattr(run, "execution_mode", None) == "live_provider"
        and getattr(run, "live_inference_status", None) == "completed"
        and getattr(run, "real_inference_requests_sent", 0) > 0
        and getattr(receipt, "execution_mode", None) == "live_provider"
        and getattr(receipt, "live_inference_status", None) == "completed"
        and getattr(receipt, "real_inference_requests_sent", 0) > 0
        and receipt.cost.model_calls == receipt.real_inference_requests_sent
    )


def _authorize_paired_retention(
    *,
    epoch: ResearchEpochManifest,
    parent_proofs: Sequence[EvaluatorOutcomeProofBinding],
    child_proofs: Sequence[EvaluatorOutcomeProofBinding],
    expected_profile_digest: str | None,
) -> PromotionAuthorization:
    if not epoch.promotion_capable or epoch.capability_epoch != "repo-repair-v1":
        _refuse("research epoch is not promotion-capable")
    parent = _proof_map(
        parent_proofs,
        label="parent",
        epoch=epoch,
        expected_profile_digest=expected_profile_digest,
    )
    child = _proof_map(
        child_proofs,
        label="child",
        epoch=epoch,
        expected_profile_digest=expected_profile_digest,
    )
    if set(parent) != set(child):
        _refuse("parent and child proofs are not explicitly paired by PairKey")
    if len(parent) < epoch.promotion_margins.minimum_paired_receipts:
        _refuse("insufficient paired proofs for the frozen promotion margin")

    parent_protocols = {
        binding.outcome_receipt.protocol_digest for binding in parent.values()
    }
    child_protocols = {
        binding.outcome_receipt.protocol_digest for binding in child.values()
    }
    if len(parent_protocols) != 1 or len(child_protocols) != 1:
        _refuse("each promotion side must identify exactly one protocol")
    parent_protocol = next(iter(parent_protocols))
    child_protocol = next(iter(child_protocols))
    if parent_protocol == child_protocol:
        _refuse("capability promotion requires a semantic child protocol")

    gains = 0
    regressions = 0
    ordered_keys = sorted(parent)
    paired_identity_fields = (
        "task_manifest_id",
        "task_manifest_digest",
        "evaluation_contract_id",
        "evaluation_contract_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "profile_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
        "evaluator_environment_digest",
    )
    for key in ordered_keys:
        parent_receipt = parent[key].outcome_receipt
        child_receipt = child[key].outcome_receipt
        crossed = [
            field_name
            for field_name in paired_identity_fields
            if getattr(parent_receipt, field_name) != getattr(child_receipt, field_name)
        ]
        if crossed:
            _refuse("paired outcome identities crossed: " + ", ".join(crossed))
        if child_receipt.complete_repair and not parent_receipt.complete_repair:
            gains += 1
        elif parent_receipt.complete_repair and not child_receipt.complete_repair:
            regressions += 1

    if regressions > epoch.promotion_margins.maximum_pair_regressions:
        _refuse("child exceeds the frozen paired-regression margin")
    net_gain = gains - regressions
    if net_gain < epoch.promotion_margins.minimum_complete_repair_gain:
        _refuse("diagnostics/process quality cannot replace complete-repair improvement")

    parent_receipt_digests = tuple(
        parent[key].outcome_receipt.receipt_digest for key in ordered_keys
    )
    child_receipt_digests = tuple(
        child[key].outcome_receipt.receipt_digest for key in ordered_keys
    )
    parent_proof_digests = tuple(parent[key].binding_digest for key in ordered_keys)
    child_proof_digests = tuple(child[key].binding_digest for key in ordered_keys)
    live_authorized = all(
        _live_capability_proof(binding)
        for binding in (*parent.values(), *child.values())
    )
    reason = (
        "paired improvement is backed by live-provider RunEvidence"
        if live_authorized
        else (
            "paired improvement is implementation evidence only; "
            "live-provider RunEvidence is required for capability promotion"
        )
    )
    authorization_id = "retention." + evidence_digest(
        {
            "epoch": epoch.epoch_manifest_digest,
            "parent_proofs": parent_proof_digests,
            "child_proofs": child_proof_digests,
            "capability_promotion_authorized": live_authorized,
        }
    )[:24]
    return PromotionAuthorization(
        authorization_id=authorization_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        parent_protocol_digest=parent_protocol,
        child_protocol_digest=child_protocol,
        pair_key_digests=tuple(ordered_keys),
        parent_receipt_digests=parent_receipt_digests,
        child_receipt_digests=child_receipt_digests,
        parent_proof_binding_digests=parent_proof_digests,
        child_proof_binding_digests=child_proof_digests,
        complete_repair_gain=net_gain,
        pair_regressions=regressions,
        capability_promotion_authorized=live_authorized,
        capability_promotion_reason=reason,
    )


def authorize_paired_search_retention(
    *,
    epoch: ResearchEpochManifest,
    parent_proofs: Sequence[EvaluatorOutcomeProofBinding],
    child_proofs: Sequence[EvaluatorOutcomeProofBinding],
    expected_profile_digest: str | None = None,
) -> PromotionAuthorization:
    """Retain an improving child while deriving, never asserting, capability status."""

    return _authorize_paired_retention(
        epoch=epoch,
        parent_proofs=parent_proofs,
        child_proofs=child_proofs,
        expected_profile_digest=expected_profile_digest,
    )


def authorize_paired_capability_promotion(
    *,
    epoch: ResearchEpochManifest,
    parent_proofs: Sequence[EvaluatorOutcomeProofBinding],
    child_proofs: Sequence[EvaluatorOutcomeProofBinding],
    expected_profile_digest: str | None = None,
) -> PromotionAuthorization:
    """Authorize capability promotion only from original live-provider proof."""

    authorization = _authorize_paired_retention(
        epoch=epoch,
        parent_proofs=parent_proofs,
        child_proofs=child_proofs,
        expected_profile_digest=expected_profile_digest,
    )
    if not authorization.capability_promotion_authorized:
        _refuse(authorization.capability_promotion_reason)
    return authorization


__all__ = [
    "PromotionAuthorization",
    "PromotionRefusal",
    "assert_authoritative_outcome_receipt",
    "assert_authoritative_outcome_proof",
    "authorize_paired_capability_promotion",
    "authorize_paired_search_retention",
]
