from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...contracts.harness import (
    ArtifactDeliveryPlan,
    ArtifactWritePlan,
    CompositeRunPlan,
)
from ...core.identity import canonical_identity_digest


class ArtifactStoreError(RuntimeError):
    pass


def artifact_payload_digest(payload: str) -> str:
    return canonical_identity_digest(payload, domain="runtime-artifact-payload")


class StoredArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    channel_id: str
    producer_call_id: str
    payload_kind: Literal["text"] = "text"
    payload: str
    payload_digest: str
    byte_size: int = Field(ge=0)
    max_bytes: int = Field(gt=0)
    immutable: Literal[True] = True
    visibility: Literal["directed"] = "directed"
    schema_id: Literal["text.utf8.v1"] = "text.utf8.v1"
    intended_consumer_call_ids: tuple[str, ...]


class ArtifactDeliveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str
    artifact_id: str
    producer_call_id: str
    consumer_call_id: str
    payload: str
    payload_digest: str
    byte_size: int = Field(ge=0)


class ArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: StoredArtifact
    actual_consumer_call_ids: tuple[str, ...]


class ImmutableArtifactStore:
    """Run-local write-once text artifacts with exact directed delivery."""

    def __init__(self, plan: CompositeRunPlan, *, max_total_bytes: int) -> None:
        if max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be positive")
        self.max_total_bytes = max_total_bytes
        self._write_plans: dict[str, ArtifactWritePlan] = {}
        for call in plan.actor_calls:
            for write in call.artifact_writes:
                if write.artifact_id in self._write_plans:
                    raise ArtifactStoreError(
                        f"artifact {write.artifact_id!r} has multiple write declarations"
                    )
                self._write_plans[write.artifact_id] = write

        self._delivery_plans: dict[tuple[str, str], ArtifactDeliveryPlan] = {}
        intended: dict[str, list[str]] = defaultdict(list)
        for delivery in plan.artifact_deliveries:
            key = (delivery.artifact_id, delivery.consumer_call_id)
            if key in self._delivery_plans:
                raise ArtifactStoreError(
                    f"artifact {delivery.artifact_id!r} has a duplicate delivery"
                )
            write = self._write_plans.get(delivery.artifact_id)
            if write is None:
                raise ArtifactStoreError(
                    f"delivery references undeclared artifact {delivery.artifact_id!r}"
                )
            if (
                delivery.channel_id != write.channel_id
                or delivery.producer_call_id != write.producer_call_id
            ):
                raise ArtifactStoreError(
                    f"delivery for {delivery.artifact_id!r} does not match its write declaration"
                )
            self._delivery_plans[key] = delivery
            intended[delivery.artifact_id].append(delivery.consumer_call_id)
        for artifact_id in self._write_plans:
            if not intended[artifact_id]:
                raise ArtifactStoreError(
                    f"declared artifact {artifact_id!r} has no intended consumer"
                )

        self._intended = {
            artifact_id: tuple(consumers)
            for artifact_id, consumers in intended.items()
        }
        self._artifacts: dict[str, StoredArtifact] = {}
        self._deliveries: list[ArtifactDeliveryEvidence] = []
        self._delivered_keys: set[tuple[str, str]] = set()
        self._actual_readers: dict[str, list[str]] = defaultdict(list)
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def write(
        self,
        write_plan: ArtifactWritePlan,
        *,
        producer_call_id: str,
        payload: str,
    ) -> StoredArtifact:
        declared = self._write_plans.get(write_plan.artifact_id)
        if declared is None or declared != write_plan:
            raise ArtifactStoreError(
                f"artifact write {write_plan.artifact_id!r} was not declared by the run plan"
            )
        if producer_call_id != declared.producer_call_id:
            raise ArtifactStoreError(
                f"call {producer_call_id!r} cannot produce artifact {declared.artifact_id!r}"
            )
        if declared.artifact_id in self._artifacts:
            raise ArtifactStoreError(f"artifact {declared.artifact_id!r} is immutable")
        if not isinstance(payload, str):
            raise ArtifactStoreError("repo-repair-v1 artifacts must be text")
        byte_size = len(payload.encode("utf-8"))
        if byte_size > declared.max_bytes:
            raise ArtifactStoreError(
                f"artifact {declared.artifact_id!r} exceeds its {declared.max_bytes}-byte limit"
            )
        if self._total_bytes + byte_size > self.max_total_bytes:
            raise ArtifactStoreError("aggregate artifact byte ceiling exceeded")
        artifact = StoredArtifact(
            artifact_id=declared.artifact_id,
            channel_id=declared.channel_id,
            producer_call_id=producer_call_id,
            payload=payload,
            payload_digest=artifact_payload_digest(payload),
            byte_size=byte_size,
            max_bytes=declared.max_bytes,
            intended_consumer_call_ids=self._intended[declared.artifact_id],
        )
        self._artifacts[artifact.artifact_id] = artifact
        self._total_bytes += byte_size
        return artifact

    def deliver(
        self,
        *,
        artifact_id: str,
        consumer_call_id: str,
    ) -> ArtifactDeliveryEvidence:
        key = (artifact_id, consumer_call_id)
        delivery = self._delivery_plans.get(key)
        if delivery is None:
            raise ArtifactStoreError(
                f"artifact {artifact_id!r} is not declared for call {consumer_call_id!r}"
            )
        if key in self._delivered_keys:
            raise ArtifactStoreError(
                f"artifact {artifact_id!r} was already delivered to {consumer_call_id!r}"
            )
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            raise ArtifactStoreError(
                f"required artifact {artifact_id!r} has not been produced"
            )
        evidence = ArtifactDeliveryEvidence(
            channel_id=delivery.channel_id,
            artifact_id=artifact.artifact_id,
            producer_call_id=artifact.producer_call_id,
            consumer_call_id=consumer_call_id,
            payload=artifact.payload,
            payload_digest=artifact.payload_digest,
            byte_size=artifact.byte_size,
        )
        self._deliveries.append(evidence)
        self._delivered_keys.add(key)
        self._actual_readers[artifact_id].append(consumer_call_id)
        return evidence

    def artifact(self, artifact_id: str) -> StoredArtifact:
        try:
            return self._artifacts[artifact_id]
        except KeyError as exc:
            raise ArtifactStoreError(f"artifact {artifact_id!r} does not exist") from exc

    def was_delivered(self, *, artifact_id: str, consumer_call_id: str) -> bool:
        return (artifact_id, consumer_call_id) in self._delivered_keys

    def read_retained(self, *, artifact_id: str, consumer_call_id: str) -> StoredArtifact:
        artifact = self.artifact(artifact_id)
        if consumer_call_id in self._actual_readers[artifact_id]:
            raise ArtifactStoreError(
                f"artifact {artifact_id!r} was already read by {consumer_call_id!r}"
            )
        self._actual_readers[artifact_id].append(consumer_call_id)
        return artifact

    def deliveries(self) -> tuple[ArtifactDeliveryEvidence, ...]:
        return tuple(self._deliveries)

    def evidence(self) -> tuple[ArtifactEvidence, ...]:
        return tuple(
            ArtifactEvidence(
                artifact=self._artifacts[artifact_id],
                actual_consumer_call_ids=tuple(self._actual_readers[artifact_id]),
            )
            for artifact_id in self._artifacts
        )


__all__ = [
    "ArtifactDeliveryEvidence",
    "ArtifactEvidence",
    "ArtifactStoreError",
    "ImmutableArtifactStore",
    "StoredArtifact",
    "artifact_payload_digest",
]
