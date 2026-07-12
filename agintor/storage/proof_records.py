from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ..authority.public_tasks import assert_public_payload
from ..contracts.outcomes import pair_key_digest
from ..contracts.promotion_proof import (
    EvaluatorOutcomeProofBinding,
    bind_evaluator_outcome_proof,
)
from ..contracts.run_evidence import (
    ProofPathPolicy,
    RunProofRecord,
    run_evidence_public_projection,
)
from ..core.versioning import RUNTIME_CONTRACT_VERSION


PROOF_STORE_SCHEMA_VERSION = "repo-repair-proof-store-v1"


class ProofStoreError(RuntimeError):
    pass


def _sha256_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ProofStoreError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


class ProofStoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class ProofStoreManifest(ProofStoreModel):
    schema_version: Literal[PROOF_STORE_SCHEMA_VERSION] = PROOF_STORE_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    single_writer: Literal[True] = True
    append_only: Literal[True] = True
    path_policy: ProofPathPolicy = ProofPathPolicy()


class OutcomeProofLink(ProofStoreModel):
    outcome_receipt_digest: str
    proof_record_digest: str
    run_evidence_digest: str
    pair_key_digest: str
    record_path: str

    @field_validator(
        "outcome_receipt_digest",
        "proof_record_digest",
        "run_evidence_digest",
        "pair_key_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("proof link identities must be lowercase SHA-256 digests")
        return normalized

    @field_validator("record_path")
    @classmethod
    def validate_record_path(cls, value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        path = Path(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError("proof link record_path must be store-relative")
        return normalized


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


class ImmutableProofRecordStore:
    """The sole append-only writer for canonical V1 run proof records."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.records_root = self.root / "runs"
        self.links_root = self.root / "outcome_links"
        self.manifest_path = self.root / "store_manifest.json"
        self.lock_path = self.root / ".single_writer.lock"

    @contextmanager
    def _writer(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_BINARY", 0),
            )
        except FileExistsError as exc:
            raise ProofStoreError("proof store already has an active writer") from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.fsync(descriptor)
            yield
        finally:
            os.close(descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_once(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_BINARY", 0),
            )
        except FileExistsError:
            existing = path.read_bytes()
            if existing != payload:
                raise ProofStoreError(f"immutable proof path already contains different bytes: {path}")
            return
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ProofStoreError(f"failed to append immutable proof file: {path}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_manifest(self) -> None:
        expected = _canonical_bytes(ProofStoreManifest().model_dump(mode="json"))
        self._write_once(self.manifest_path, expected)

    def _record_relative_path(self, record: RunProofRecord) -> Path:
        evidence = record.run_evidence
        return (
            Path("runs")
            / pair_key_digest(evidence.pair_key)
            / evidence.protocol_digest
            / f"{evidence.evidence_digest}.json"
        )

    def append(self, record: RunProofRecord) -> Path:
        canonical = RunProofRecord.model_validate(record.model_dump(mode="python"))
        relative_path = self._record_relative_path(canonical)
        record_path = self.root / relative_path
        payload = _canonical_bytes(canonical.model_dump(mode="json", exclude_none=True))
        with self._writer():
            self._ensure_manifest()
            self._write_once(record_path, payload)
            if canonical.outcome_receipt is not None:
                receipt = canonical.outcome_receipt
                link = OutcomeProofLink(
                    outcome_receipt_digest=receipt.receipt_digest,
                    proof_record_digest=canonical.proof_record_digest,
                    run_evidence_digest=canonical.run_evidence.evidence_digest,
                    pair_key_digest=pair_key_digest(receipt.pair_key),
                    record_path=relative_path.as_posix(),
                )
                self._write_once(
                    self.links_root / f"{receipt.receipt_digest}.json",
                    _canonical_bytes(link.model_dump(mode="json")),
                )
        return record_path

    def load(self, record_path: str | Path) -> RunProofRecord:
        source = Path(record_path)
        if not source.is_absolute():
            source = self.root / source
        resolved = source.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ProofStoreError("proof record path escapes the store root") from exc
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        return RunProofRecord.model_validate(payload)

    def lookup_outcome(self, outcome_receipt_digest: str) -> RunProofRecord:
        digest = _sha256_digest(
            outcome_receipt_digest,
            "outcome_receipt_digest",
        )
        link_path = self.links_root / f"{digest}.json"
        link = OutcomeProofLink.model_validate(
            json.loads(link_path.read_text(encoding="utf-8"))
        )
        record = self.load(link.record_path)
        if record.proof_record_digest != link.proof_record_digest:
            raise ProofStoreError("outcome link crossed proof record digest")
        if record.run_evidence.evidence_digest != link.run_evidence_digest:
            raise ProofStoreError("outcome link crossed run evidence digest")
        if record.outcome_receipt is None or record.outcome_receipt.receipt_digest != digest:
            raise ProofStoreError("outcome link does not resolve to its receipt")
        return record

    def verify_outcome_proof_binding(
        self,
        binding: EvaluatorOutcomeProofBinding,
    ) -> EvaluatorOutcomeProofBinding:
        """Resolve a public binding against the evaluator-owned immutable store."""

        canonical = EvaluatorOutcomeProofBinding.model_validate(
            binding.model_dump(mode="python")
        )
        record = self.lookup_outcome(canonical.outcome_receipt.receipt_digest)
        expected = bind_evaluator_outcome_proof(
            record,
            proof_record_ref=self._record_relative_path(record).as_posix(),
            outcome_link_ref=(
                f"outcome_links/{canonical.outcome_receipt.receipt_digest}.json"
            ),
        )
        if canonical != expected:
            raise ProofStoreError(
                "public outcome proof binding differs from evaluator proof-store authority"
            )
        return expected

    def iter_records(self) -> Iterator[RunProofRecord]:
        if not self.records_root.exists():
            return
        for path in sorted(self.records_root.rglob("*.json")):
            yield self.load(path)


def proof_record_public_projection(record: RunProofRecord) -> dict[str, Any]:
    payload = {
        "schema_version": record.schema_version,
        "runtime_contract_version": record.runtime_contract_version,
        "proof_record_id": record.proof_record_id,
        "proof_record_digest": record.proof_record_digest,
        "path_policy": record.path_policy.model_dump(mode="json"),
        "run_evidence": run_evidence_public_projection(record.run_evidence),
        "outcome_receipt_digest": (
            record.outcome_receipt.receipt_digest
            if record.outcome_receipt is not None
            else None
        ),
    }
    assert_public_payload(payload)
    return payload


__all__ = [
    "ImmutableProofRecordStore",
    "OutcomeProofLink",
    "PROOF_STORE_SCHEMA_VERSION",
    "ProofStoreError",
    "ProofStoreManifest",
    "proof_record_public_projection",
]
