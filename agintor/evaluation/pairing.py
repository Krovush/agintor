from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts.epochs import ResearchEpochManifest
from ..contracts.outcomes import OutcomeReceipt, PairKey, pair_key_digest
from ..core.identity import evidence_digest
from ..search.promotion import (
    PromotionRefusal,
    assert_authoritative_outcome_receipt,
)


PAIR_JOIN_SCHEMA_VERSION = "repo-repair-pair-join-v1"


class PairingError(ValueError):
    """Raised when an exact outcome panel cannot be joined safely."""


class PairingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class JoinedOutcomePair(PairingModel):
    pair_key: PairKey
    pair_key_digest: str
    parent_receipt: OutcomeReceipt
    child_receipt: OutcomeReceipt

    @model_validator(mode="after")
    def validate_pair(self) -> "JoinedOutcomePair":
        computed = pair_key_digest(self.pair_key)
        if self.pair_key_digest != computed:
            raise ValueError("JoinedOutcomePair pair_key_digest mismatch")
        if self.parent_receipt.pair_key != self.pair_key:
            raise ValueError("parent receipt crossed JoinedOutcomePair PairKey")
        if self.child_receipt.pair_key != self.pair_key:
            raise ValueError("child receipt crossed JoinedOutcomePair PairKey")
        return self


class JoinedOutcomePanel(PairingModel):
    schema_version: Literal[PAIR_JOIN_SCHEMA_VERSION] = PAIR_JOIN_SCHEMA_VERSION
    join_id: str
    join_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    parent_protocol_digest: str
    child_protocol_digest: str
    expected_pair_key_digests: tuple[str, ...] = Field(min_length=1)
    pairs: tuple[JoinedOutcomePair, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_panel(self) -> "JoinedOutcomePanel":
        actual = tuple(pair.pair_key_digest for pair in self.pairs)
        if actual != self.expected_pair_key_digests:
            raise ValueError("joined pairs do not exactly cover expected PairKeys")
        if actual != tuple(sorted(actual)) or len(actual) != len(set(actual)):
            raise ValueError("joined PairKeys must be unique and canonical")
        payload = self.model_dump(mode="python", exclude={"join_digest"})
        computed = evidence_digest({"kind": PAIR_JOIN_SCHEMA_VERSION, **payload})
        if self.join_digest and self.join_digest != computed:
            raise ValueError("join_digest does not match paired outcome panel")
        if not self.join_digest:
            object.__setattr__(self, "join_digest", computed)
        return self


def _receipt_map(
    receipts: Sequence[OutcomeReceipt],
    *,
    side: str,
    epoch: ResearchEpochManifest,
) -> dict[str, OutcomeReceipt]:
    mapped: dict[str, OutcomeReceipt] = {}
    for receipt in receipts:
        try:
            assert_authoritative_outcome_receipt(receipt, epoch)
        except PromotionRefusal as exc:
            raise PairingError(f"unhealthy or unauthorized {side} outcome: {exc}") from exc
        digest = pair_key_digest(receipt.pair_key)
        if digest in mapped:
            raise PairingError(f"duplicate {side} outcome for PairKey {digest}")
        mapped[digest] = receipt
    return mapped


def _expected_map(
    pair_keys: Sequence[PairKey],
    *,
    epoch: ResearchEpochManifest,
) -> dict[str, PairKey]:
    if not pair_keys:
        raise PairingError("an explicit nonempty expected PairKey panel is required")
    expected: dict[str, PairKey] = {}
    for pair_key in pair_keys:
        if pair_key.provider_config_digest != epoch.deployment.provider_config_digest:
            raise PairingError("expected PairKey provider configuration crossed the epoch")
        digest = pair_key_digest(pair_key)
        if digest in expected:
            raise PairingError(f"duplicate expected PairKey {digest}")
        expected[digest] = pair_key
    return expected


def _assert_pair_configuration(
    parent: OutcomeReceipt,
    child: OutcomeReceipt,
) -> None:
    fields = (
        "runtime_contract_version",
        "capability_epoch",
        "data_state",
        "epoch_id",
        "epoch_manifest_digest",
        "release_digest",
        "release_manifest_digest",
        "profile_digest",
        "split_manifest_digest",
        "task_manifest_id",
        "task_manifest_digest",
        "evaluation_contract_id",
        "evaluation_contract_digest",
        "evaluator_id",
        "evaluator_identity_digest",
        "evaluation_policy_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
        "evaluator_environment_digest",
    )
    crossed = [
        field_name
        for field_name in fields
        if getattr(parent, field_name) != getattr(child, field_name)
    ]
    if crossed:
        raise PairingError(
            "parent/child configuration mismatch for " + ", ".join(crossed)
        )


def join_outcome_receipts(
    *,
    epoch: ResearchEpochManifest,
    expected_pair_keys: Sequence[PairKey],
    parent_receipts: Sequence[OutcomeReceipt],
    child_receipts: Sequence[OutcomeReceipt],
) -> JoinedOutcomePanel:
    """Join exact evaluator outcomes by PairKey, never by collection position."""

    expected = _expected_map(expected_pair_keys, epoch=epoch)
    parent = _receipt_map(parent_receipts, side="parent", epoch=epoch)
    child = _receipt_map(child_receipts, side="child", epoch=epoch)
    expected_keys = set(expected)
    for side, mapped in (("parent", parent), ("child", child)):
        missing = sorted(expected_keys - set(mapped))
        unexpected = sorted(set(mapped) - expected_keys)
        if missing or unexpected:
            details: list[str] = []
            if missing:
                details.append(f"missing={missing}")
            if unexpected:
                details.append(f"unexpected={unexpected}")
            raise PairingError(f"{side} PairKey coverage mismatch: " + " ".join(details))

    parent_protocols = {receipt.protocol_digest for receipt in parent.values()}
    child_protocols = {receipt.protocol_digest for receipt in child.values()}
    if len(parent_protocols) != 1 or len(child_protocols) != 1:
        raise PairingError("each outcome side must identify exactly one protocol")
    parent_protocol = next(iter(parent_protocols))
    child_protocol = next(iter(child_protocols))

    ordered_keys = tuple(sorted(expected))
    pairs: list[JoinedOutcomePair] = []
    for digest in ordered_keys:
        parent_receipt = parent[digest]
        child_receipt = child[digest]
        _assert_pair_configuration(parent_receipt, child_receipt)
        pairs.append(
            JoinedOutcomePair(
                pair_key=expected[digest],
                pair_key_digest=digest,
                parent_receipt=parent_receipt,
                child_receipt=child_receipt,
            )
        )
    join_id = "pair-join." + evidence_digest(
        {
            "epoch": epoch.epoch_manifest_digest,
            "parent": [pair.parent_receipt.receipt_digest for pair in pairs],
            "child": [pair.child_receipt.receipt_digest for pair in pairs],
        }
    )[:24]
    return JoinedOutcomePanel(
        join_id=join_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        parent_protocol_digest=parent_protocol,
        child_protocol_digest=child_protocol,
        expected_pair_key_digests=ordered_keys,
        pairs=tuple(pairs),
    )


__all__ = [
    "JoinedOutcomePair",
    "JoinedOutcomePanel",
    "PAIR_JOIN_SCHEMA_VERSION",
    "PairingError",
    "join_outcome_receipts",
]
