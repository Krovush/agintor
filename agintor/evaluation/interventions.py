from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..authority.public_tasks import assert_public_payload
from ..contracts.outcomes import PairKey
from ..contracts.run_evidence import ObservedValue, RunEvidence
from ..core.identity import evidence_digest


NEUTRAL_INTERVENTION_SCHEMA_VERSION = "neutral-artifact-intervention-v1"
MATCHED_INTERVENTION_PAIR_SCHEMA_VERSION = "matched-intervention-pair-v1"


class InterventionError(ValueError):
    """Raised when a neutral replacement is not protocol-valid and matched."""


class InterventionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class NeutralArtifactIntervention(InterventionModel):
    schema_version: Literal[NEUTRAL_INTERVENTION_SCHEMA_VERSION] = (
        NEUTRAL_INTERVENTION_SCHEMA_VERSION
    )
    intervention_id: str
    intervention_digest: str = ""
    source_run_evidence_digest: str
    pair_key: PairKey
    protocol_digest: str
    artifact_id: str
    channel_id: str
    producer_call_id: str
    consumer_call_id: str
    delivery_id: str
    read_id: str
    context_id: str
    context_entry_id: str
    artifact_schema: Literal["text"] = "text"
    original: ObservedValue
    neutral: ObservedValue
    original_priced_input_units: int = Field(ge=0)
    neutral_priced_input_units: int = Field(ge=0)
    schema_matched: Literal[True] = True
    serialized_length_matched: Literal[True] = True
    route_matched: Literal[True] = True
    call_count_delta: Literal[0] = 0
    priced_input_matched: Literal[True] = True

    @model_validator(mode="after")
    def validate_intervention(self) -> "NeutralArtifactIntervention":
        if not isinstance(self.original.value, str) or not isinstance(
            self.neutral.value, str
        ):
            raise ValueError("V1 neutral artifact intervention supports text only")
        if self.original.value_digest == self.neutral.value_digest:
            raise ValueError("neutral artifact must differ from original content")
        if self.original.serialized_bytes != self.neutral.serialized_bytes:
            raise ValueError("neutral artifact must preserve serialized length")
        if self.original_priced_input_units != self.neutral_priced_input_units:
            raise ValueError("neutral artifact must preserve priced input units")
        payload = self.model_dump(mode="python", exclude={"intervention_digest"})
        computed = evidence_digest(
            {"kind": NEUTRAL_INTERVENTION_SCHEMA_VERSION, **payload}
        )
        if self.intervention_digest and self.intervention_digest != computed:
            raise ValueError("intervention_digest does not match intervention")
        if not self.intervention_digest:
            object.__setattr__(self, "intervention_digest", computed)
        return self


class MatchedInterventionRunPair(InterventionModel):
    schema_version: Literal[MATCHED_INTERVENTION_PAIR_SCHEMA_VERSION] = (
        MATCHED_INTERVENTION_PAIR_SCHEMA_VERSION
    )
    matched_pair_id: str
    matched_pair_digest: str = ""
    pair_key: PairKey
    protocol_digest: str
    intervention_digest: str
    intact_run_evidence_digest: str
    neutral_run_evidence_digest: str
    provider_call_count: int = Field(gt=0)
    tool_call_count: int = Field(ge=0)
    route_count: int = Field(ge=0)
    input_tokens_per_arm: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_digest(self) -> "MatchedInterventionRunPair":
        payload = self.model_dump(mode="python", exclude={"matched_pair_digest"})
        computed = evidence_digest(
            {"kind": MATCHED_INTERVENTION_PAIR_SCHEMA_VERSION, **payload}
        )
        if self.matched_pair_digest and self.matched_pair_digest != computed:
            raise ValueError("matched_pair_digest does not match intervention pair")
        if not self.matched_pair_digest:
            object.__setattr__(self, "matched_pair_digest", computed)
        return self


def length_matched_neutral_text(original: str) -> str:
    """Create deterministic distinct text with equal canonical JSON byte length."""

    observed = ObservedValue(value=original)
    if not original:
        raise InterventionError("empty artifacts cannot have a distinct length-matched neutral")
    neutral_length = observed.serialized_bytes - 2
    if neutral_length <= 0:
        raise InterventionError("artifact is too small for a distinct neutral replacement")
    fill = "N" if set(original) != {"N"} else "Z"
    neutral = fill * neutral_length
    if ObservedValue(value=neutral).serialized_bytes != observed.serialized_bytes:
        raise InterventionError("failed to construct a serialized-length-matched neutral")
    return neutral


def build_neutral_artifact_intervention(
    *,
    source_run: RunEvidence,
    artifact_id: str,
    consumer_call_id: str,
    neutral_value: str,
    priced_input_measure: Callable[[str], int],
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> NeutralArtifactIntervention:
    """Bind a neutral value to one already-observed protocol delivery/read route."""

    if source_run.arm != "intact":
        raise InterventionError("neutral interventions must be built from an intact run")
    artifact = next(
        (item for item in source_run.artifacts if item.artifact_id == artifact_id),
        None,
    )
    if artifact is None:
        raise InterventionError("neutral intervention references an unknown artifact")
    if artifact.artifact_schema != "text":
        raise InterventionError("V1 neutral intervention requires a text artifact")
    delivery = next(
        (
            item
            for item in source_run.deliveries
            if item.artifact_id == artifact_id
            and item.consumer_call_id == consumer_call_id
        ),
        None,
    )
    read = next(
        (
            item
            for item in source_run.reads
            if item.artifact_id == artifact_id
            and item.consumer_call_id == consumer_call_id
        ),
        None,
    )
    if delivery is None or read is None or delivery.delivery_kind != "intact":
        raise InterventionError("artifact lacks one intact delivered/read route")
    if delivery.observed != artifact.observed or read.observed != artifact.observed:
        raise InterventionError("source artifact route does not preserve exact content")

    assert_public_payload(
        {"neutral_artifact": neutral_value},
        canary_values=canary_values,
        canary_digests=canary_digests,
    )
    neutral = ObservedValue(value=neutral_value)
    if neutral.serialized_bytes != artifact.observed.serialized_bytes:
        raise InterventionError("neutral artifact serialized length is not matched")
    original_units = int(priced_input_measure(str(artifact.observed.value)))
    neutral_units = int(priced_input_measure(neutral_value))
    if original_units < 0 or neutral_units < 0:
        raise InterventionError("priced-input measure may not return negative units")
    if original_units != neutral_units:
        raise InterventionError("neutral artifact priced input is not matched")

    intervention_id = "neutral." + evidence_digest(
        {
            "source": source_run.evidence_digest,
            "artifact": artifact_id,
            "consumer": consumer_call_id,
            "neutral": neutral.value_digest,
        }
    )[:24]
    return NeutralArtifactIntervention(
        intervention_id=intervention_id,
        source_run_evidence_digest=source_run.evidence_digest,
        pair_key=source_run.pair_key,
        protocol_digest=source_run.protocol_digest,
        artifact_id=artifact.artifact_id,
        channel_id=artifact.channel_id,
        producer_call_id=artifact.producer_call_id,
        consumer_call_id=consumer_call_id,
        delivery_id=delivery.delivery_id,
        read_id=read.read_id,
        context_id=read.context_id,
        context_entry_id=read.context_entry_id,
        original=artifact.observed,
        neutral=neutral,
        original_priced_input_units=original_units,
        neutral_priced_input_units=neutral_units,
    )


def join_matched_intervention_runs(
    *,
    intact_run: RunEvidence,
    neutral_run: RunEvidence,
    intervention: NeutralArtifactIntervention,
) -> MatchedInterventionRunPair:
    """Validate that intact/neutral arms differ only through the bound delivery."""

    if intact_run.arm != "intact" or neutral_run.arm != "neutral_artifact":
        raise InterventionError("matched intervention requires intact and neutral arms")
    if intact_run.evidence_digest != intervention.source_run_evidence_digest:
        raise InterventionError("intervention does not bind the intact source run")
    if neutral_run.intervention_digest != intervention.intervention_digest:
        raise InterventionError("neutral run does not bind the intervention")
    identity_fields = (
        "runtime_contract_version",
        "capability_epoch",
        "data_state",
        "epoch_id",
        "epoch_manifest_digest",
        "split_manifest_digest",
        "pair_key",
        "task_manifest_digest",
        "protocol_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "deployment_id",
        "provider",
        "model",
    )
    crossed = [
        field_name
        for field_name in identity_fields
        if getattr(intact_run, field_name) != getattr(neutral_run, field_name)
    ]
    if crossed:
        raise InterventionError(
            "matched intervention run identity mismatch: " + ", ".join(crossed)
        )

    intact_call_shape = tuple(
        (
            call.call_id,
            call.actor_id,
            call.attempt_index,
            call.deployment_id,
            call.provider,
            call.model,
            call.status,
            call.request_sent,
        )
        for call in intact_run.provider_calls
    )
    neutral_call_shape = tuple(
        (
            call.call_id,
            call.actor_id,
            call.attempt_index,
            call.deployment_id,
            call.provider,
            call.model,
            call.status,
            call.request_sent,
        )
        for call in neutral_run.provider_calls
    )
    if intact_call_shape != neutral_call_shape:
        raise InterventionError("neutral arm does not preserve provider call opportunity")
    route_shape = lambda run: tuple(
        (
            route.route_kind,
            route.from_call_id,
            route.to_call_id,
            route.stage_id,
            route.trigger,
        )
        for route in run.routes
    )
    if route_shape(intact_run) != route_shape(neutral_run):
        raise InterventionError("neutral arm does not preserve protocol routes")
    intact_cost = intact_run.cost_ledger.cost
    neutral_cost = neutral_run.cost_ledger.cost
    matched_cost_fields = (
        "model_calls",
        "input_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "tool_calls",
        "retries",
    )
    if any(
        getattr(intact_cost, field_name) != getattr(neutral_cost, field_name)
        for field_name in matched_cost_fields
    ):
        raise InterventionError("neutral arm does not preserve priced input/call envelope")

    neutral_delivery = next(
        (
            delivery
            for delivery in neutral_run.deliveries
            if delivery.artifact_id == intervention.artifact_id
            and delivery.consumer_call_id == intervention.consumer_call_id
        ),
        None,
    )
    neutral_read = next(
        (
            read
            for read in neutral_run.reads
            if read.artifact_id == intervention.artifact_id
            and read.consumer_call_id == intervention.consumer_call_id
        ),
        None,
    )
    if (
        neutral_delivery is None
        or neutral_read is None
        or neutral_delivery.delivery_kind != "neutral_replacement"
        or neutral_delivery.intervention_digest != intervention.intervention_digest
        or neutral_delivery.observed != intervention.neutral
        or neutral_read.observed != intervention.neutral
    ):
        raise InterventionError("neutral run did not deliver/read the exact replacement")

    matched_pair_id = "intervention-pair." + evidence_digest(
        {
            "intact": intact_run.evidence_digest,
            "neutral": neutral_run.evidence_digest,
            "intervention": intervention.intervention_digest,
        }
    )[:24]
    return MatchedInterventionRunPair(
        matched_pair_id=matched_pair_id,
        pair_key=intact_run.pair_key,
        protocol_digest=intact_run.protocol_digest,
        intervention_digest=intervention.intervention_digest,
        intact_run_evidence_digest=intact_run.evidence_digest,
        neutral_run_evidence_digest=neutral_run.evidence_digest,
        provider_call_count=len(intact_run.provider_calls),
        tool_call_count=len(intact_run.tool_receipts),
        route_count=len(intact_run.routes),
        input_tokens_per_arm=intact_cost.input_tokens,
    )


__all__ = [
    "InterventionError",
    "MATCHED_INTERVENTION_PAIR_SCHEMA_VERSION",
    "MatchedInterventionRunPair",
    "NEUTRAL_INTERVENTION_SCHEMA_VERSION",
    "NeutralArtifactIntervention",
    "build_neutral_artifact_intervention",
    "join_matched_intervention_runs",
    "length_matched_neutral_text",
]
