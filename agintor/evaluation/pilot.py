from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..authority.public_tasks import (
    assert_public_payload,
    sealed_canary_digest,
    task_envelope_public_projection,
)
from ..contracts.epochs import (
    DeploymentIdentity,
    ResearchEpochManifest,
    TaskCeilings,
    TaskEnvelope,
    assert_task_bound_to_epoch,
)
from ..contracts.harness import CompositeRunPlan, HarnessProtocol, RuntimeDependencyManifest
from ..contracts.outcomes import OutcomeReceipt, pair_key_digest
from ..contracts.run_evidence import RunEvidence, assert_no_resolved_credentials
from ..core.identity import canonical_identity_digest, evidence_digest
from ..factory.harness_release_contracts import (
    ActiveReleasePointer,
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HarnessReleaseManifest,
    PilotNotRunSummary,
    PublicEvidenceIndex,
    PublicSearchLineageRecord,
)
from ..factory.harness_service import HarnessFactoryBuildResult
from ..search.paired_harness import PairedHarnessSearchResult
from ..runtime.sdk.harness_executor import (
    CONTROLLED_RUN_EVIDENCE_REF,
    HARNESS_SOLVE_RESULT_FILE,
    HarnessSolveResult,
)
from ..runtime.harness_profile import (
    HarnessDeploymentProfile,
    harness_deployment_profile_digest,
)
from ..runtime.kernel.composite_provider import CredentialReference
from ..storage.harness_factory_transaction import (
    HarnessFactoryChatManifest,
    HarnessFactoryMessage,
)
from ..storage.harness_session_store import HarnessSessionManifest
from ..contracts.feasibility import DevelopmentTaskFeasibilityManifest
from .gate0 import Gate0ConformanceReport, Gate0DryRunManifest


TASK_AUDIT_SCHEMA_VERSION = "repo-repair-pilot-task-audit-v1"
PILOT_DRY_RUN_SCHEMA_VERSION = "repo-repair-pilot-dry-run-v1"
MVP_READINESS_SCHEMA_VERSION = "repo-repair-mvp-readiness-v1"
PILOT_NOT_RUN_REPORT_SCHEMA_VERSION = "repo-repair-pilot-not-run-report-v1"
PILOT_EVALUATION_CONTRACT_SCHEMA_VERSION = "repo-repair-pilot-evaluation-contract-v1"
PILOT_RAW_PAIRED_OUTCOME_SCHEMA_VERSION = "repo-repair-pilot-raw-paired-outcome-v1"
PILOT_INTERVENTION_SCHEMA_VERSION = "repo-repair-pilot-intervention-v1"
PILOT_GATE_TEST_EVIDENCE_SCHEMA_VERSION = "repo-repair-pilot-gate-test-evidence-v1"

PUBLIC_RELEASE_EVIDENCE_DIR = "public_release_evidence"
CONTROLLED_EVIDENCE_DIR = "controlled_development_and_evaluator_evidence"
MVP_READINESS_MANIFEST_PATH = "mvp_readiness_packet.json"
CONTROLLED_RUN_EVIDENCE_PACKET_PATH = (
    f"{CONTROLLED_EVIDENCE_DIR}/runs/{{pair_key_digest}}/{CONTROLLED_RUN_EVIDENCE_REF}"
)
HARNESS_SOLVE_RESULT_PACKET_PATH = (
    f"{CONTROLLED_EVIDENCE_DIR}/runs/{{pair_key_digest}}/{HARNESS_SOLVE_RESULT_FILE}"
)

REQUIRED_MVP_GATES = (
    "B0",
    "H0",
    "A0a",
    "A0b",
    "A1",
    "R1a",
    "R1b",
    "I0",
    "R2",
    "E1",
    "O1",
    "G0",
    "D0",
    "M1",
    "S1",
    "F1a",
    "F1b",
    "F1c",
)

REQUIRED_PUBLIC_PATHS = frozenset(
    {
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/release_manifest.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/capability_epoch_public.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/protocol/source.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/protocol/compiled_plan.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/protocol/consumed_field_liveness_manifest.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/runtime/dependency_manifest.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/search/transaction_lineage_public.jsonl",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/search/selection_decisions_public.jsonl",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/gate0_preregistration.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/gate0_report.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/pilot_summary.json",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/limitations.md",
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/evidence_index.json",
    }
)

REQUIRED_CONTROLLED_PATHS = frozenset(
    {
        f"{CONTROLLED_EVIDENCE_DIR}/evaluation_contract.json",
        f"{CONTROLLED_EVIDENCE_DIR}/task_public_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/evaluator/task_audit_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/evaluator/outcome_receipts.jsonl",
        f"{CONTROLLED_EVIDENCE_DIR}/gate0/dry_run_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/gate0/deterministic_conformance_report.json",
        f"{CONTROLLED_EVIDENCE_DIR}/d0/fixture_feasibility_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/d0/fixture_feasibility.json",
        f"{CONTROLLED_EVIDENCE_DIR}/search/s1_offline_retention.json",
        f"{CONTROLLED_EVIDENCE_DIR}/pilot/compiled_plan.json",
        f"{CONTROLLED_EVIDENCE_DIR}/pilot/dry_run_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/factory/chat_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/factory/followup_message.json",
        f"{CONTROLLED_EVIDENCE_DIR}/factory/followup_transaction_identity.json",
        f"{CONTROLLED_EVIDENCE_DIR}/sessions/same_release_continuation.json",
        f"{CONTROLLED_EVIDENCE_DIR}/sessions/independent_new_session.json",
        f"{CONTROLLED_EVIDENCE_DIR}/sessions/session_identities.json",
        f"{CONTROLLED_EVIDENCE_DIR}/interventions/content_null_manifest.json",
        f"{CONTROLLED_EVIDENCE_DIR}/analysis/raw_paired_outcomes.jsonl",
        f"{CONTROLLED_EVIDENCE_DIR}/analysis/offline_solve_execution_provenance.json",
        f"{CONTROLLED_EVIDENCE_DIR}/analysis/pilot_report.json",
    }
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'(=])(?:[A-Za-z]:[\\/])")
_POSIX_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'(=])/(?!/)")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class PilotEvidenceError(ValueError):
    """Raised when pilot/readiness evidence crosses an authority or identity boundary."""


class PilotEvidenceModel(BaseModel):
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


def _relative_path(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a safe packet-relative path")
    return path.as_posix()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _looks_like_absolute_source_path(value: str) -> bool:
    stripped = value.strip()
    return bool(
        stripped
        and (
            _WINDOWS_ABSOLUTE_RE.search(stripped)
            or _POSIX_ABSOLUTE_RE.search(stripped)
            or "file://" in stripped.casefold()
        )
    )


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


def _scan_packet_value(
    value: Any,
    *,
    public: bool,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
    allowed_absolute_paths: Sequence[str] = (),
) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=True)
    assert_no_resolved_credentials(value)
    if public:
        assert_public_payload(
            value,
            canary_values=canary_values,
            canary_digests=canary_digests,
        )
    normalized_canary_digests = {
        str(item).strip().lower() for item in canary_digests if str(item).strip()
    }
    text_canaries = tuple(
        item for item in canary_values if isinstance(item, str) and item
    )
    byte_canaries = tuple(
        bytes(item) for item in canary_values if isinstance(item, bytes) and item
    )
    allowed_paths = {str(item) for item in allowed_absolute_paths}
    for scalar in _iter_scalars(value):
        if isinstance(scalar, str):
            if _looks_like_absolute_source_path(scalar) and scalar not in allowed_paths:
                raise ValueError("evidence packet contains an absolute source path")
            if any(pattern.search(scalar) for pattern in _SECRET_VALUE_PATTERNS):
                raise ValueError("evidence packet contains resolved credential material")
            if any(marker in scalar for marker in text_canaries):
                raise ValueError("evidence packet contains a sealed canary")
            normalized_key = re.sub(r"[^a-z0-9]+", "_", scalar.casefold()).strip("_")
            if "canary" in normalized_key:
                raise ValueError("evidence packet may not name or carry canary material")
        elif isinstance(scalar, (bytes, bytearray)):
            if any(marker in bytes(scalar) for marker in byte_canaries):
                raise ValueError("evidence packet contains a sealed canary")
        if normalized_canary_digests and sealed_canary_digest(scalar) in normalized_canary_digests:
            raise ValueError("evidence packet contains a sealed canary digest")


class AuditedDevelopmentTask(PilotEvidenceModel):
    audit_sequence: int = Field(ge=0)
    task_manifest_id: str
    task_manifest_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    split_manifest_digest: str
    data_state: Literal["development"] = "development"
    permanently_development: Literal[True] = True
    public_projection: dict[str, Any]
    public_projection_digest: str = ""
    inspected_at_ms: int = Field(ge=0)

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

    @model_validator(mode="after")
    def validate_public_task(self) -> "AuditedDevelopmentTask":
        task = TaskEnvelope.model_validate(self.public_projection)
        _scan_packet_value(
            self.public_projection,
            public=True,
            allowed_absolute_paths=(task.workspace_snapshot.uri,),
        )
        if task.data_state != "development":
            raise ValueError("sealed-confirmation tasks may not enter a pilot task audit")
        expected = {
            "task_manifest_id": task.task_manifest_id,
            "task_manifest_digest": task.task_manifest_digest,
            "epoch_id": task.epoch_id,
            "epoch_manifest_digest": task.epoch_manifest_digest,
            "split_manifest_digest": task.split_manifest_digest,
        }
        crossed = [
            name for name, expected_value in expected.items() if getattr(self, name) != expected_value
        ]
        if crossed:
            raise ValueError("audited public projection crossed task identity: " + ", ".join(crossed))
        canonical_projection = task_envelope_public_projection(task)
        if self.public_projection != canonical_projection:
            raise ValueError("task audit stores only the canonical public TaskEnvelope projection")
        computed = canonical_identity_digest(
            canonical_projection,
            domain="pilot-public-task-projection",
        )
        if self.public_projection_digest and self.public_projection_digest != computed:
            raise ValueError("public task projection digest mismatch")
        if not self.public_projection_digest:
            object.__setattr__(self, "public_projection_digest", computed)
        return self


class PilotReservationEvent(PilotEvidenceModel):
    sequence: int = Field(ge=0, le=2)
    event: Literal["reserved", "consumed", "reclassified_development"]
    pilot_id: str
    task_manifest_id: str
    task_manifest_digest: str
    occurred_at_ms: int = Field(ge=0)
    consumption_evidence_digest: str | None = None
    non_confirmatory: Literal[True] = True

    @field_validator("pilot_id", "task_manifest_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("task_manifest_digest")
    @classmethod
    def validate_task_digest(cls, value: str) -> str:
        return _require_digest(value, "task_manifest_digest")

    @field_validator("consumption_evidence_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "consumption_evidence_digest")

    @model_validator(mode="after")
    def validate_event(self) -> "PilotReservationEvent":
        expected_sequence = {
            "reserved": 0,
            "consumed": 1,
            "reclassified_development": 2,
        }[self.event]
        if self.sequence != expected_sequence:
            raise ValueError("pilot reservation event sequence is not append-only")
        if self.event == "reserved" and self.consumption_evidence_digest is not None:
            raise ValueError("reservation cannot claim consumption evidence")
        if self.event != "reserved" and self.consumption_evidence_digest is None:
            raise ValueError("consumption and reclassification require exact evidence identity")
        return self


class PilotTaskAuditManifest(PilotEvidenceModel):
    schema_version: Literal[TASK_AUDIT_SCHEMA_VERSION] = TASK_AUDIT_SCHEMA_VERSION
    audit_id: str
    audit_manifest_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    tasks: tuple[AuditedDevelopmentTask, ...] = Field(min_length=1)
    reservation_events: tuple[PilotReservationEvent, ...] = ()
    reservation_state: Literal[
        "none",
        "reserved",
        "consumed_reclassified_development",
    ] = "none"
    inspected_task_count: int = Field(gt=0)
    reserved_task_count: int = Field(default=0, ge=0, le=1)
    consumed_task_count: int = Field(default=0, ge=0, le=1)
    sealed_confirmation_tasks_inspected: Literal[0] = 0
    evaluator_payloads_inspected: Literal[0] = 0

    @field_validator("audit_id", "epoch_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("epoch_manifest_digest")
    @classmethod
    def validate_epoch_digest(cls, value: str) -> str:
        return _require_digest(value, "epoch_manifest_digest")

    @model_validator(mode="after")
    def validate_audit(self) -> "PilotTaskAuditManifest":
        if self.inspected_task_count != len(self.tasks):
            raise ValueError("inspected_task_count must equal the exact public audit records")
        sequences = [task.audit_sequence for task in self.tasks]
        if sequences != list(range(len(self.tasks))):
            raise ValueError("task audit sequences must be contiguous and ordered")
        task_digests = [task.task_manifest_digest for task in self.tasks]
        if len(task_digests) != len(set(task_digests)):
            raise ValueError("a task may be inspected only once in an audit")
        for task in self.tasks:
            if task.epoch_id != self.epoch_id or task.epoch_manifest_digest != self.epoch_manifest_digest:
                raise ValueError("audited task crossed the audit epoch")

        expected_events: tuple[str, ...]
        expected_counts: tuple[int, int]
        if self.reservation_state == "none":
            expected_events = ()
            expected_counts = (0, 0)
        elif self.reservation_state == "reserved":
            expected_events = ("reserved",)
            expected_counts = (1, 0)
        else:
            expected_events = ("reserved", "consumed", "reclassified_development")
            expected_counts = (1, 1)
        if tuple(event.event for event in self.reservation_events) != expected_events:
            raise ValueError("pilot reservation history must be an irreversible lifecycle prefix")
        if (self.reserved_task_count, self.consumed_task_count) != expected_counts:
            raise ValueError("pilot reservation counts disagree with append-only history")
        if self.reservation_events:
            first = self.reservation_events[0]
            if first.task_manifest_digest not in set(task_digests):
                raise ValueError("only an audited task may be reserved for the pilot")
            identities = {
                (event.pilot_id, event.task_manifest_id, event.task_manifest_digest)
                for event in self.reservation_events
            }
            if len(identities) != 1:
                raise ValueError("pilot reservation history crossed task or pilot identity")
            times = [event.occurred_at_ms for event in self.reservation_events]
            if times != sorted(times):
                raise ValueError("pilot reservation lifecycle timestamps may not regress")
            if len(self.reservation_events) == 3:
                if self.reservation_events[1].consumption_evidence_digest != self.reservation_events[2].consumption_evidence_digest:
                    raise ValueError("development reclassification must bind the one consumption")
                if self.reservation_events[1].occurred_at_ms != self.reservation_events[2].occurred_at_ms:
                    raise ValueError("pilot consumption must immediately reclassify the task development")

        computed = evidence_digest(
            {
                "kind": TASK_AUDIT_SCHEMA_VERSION,
                "audit": self.model_dump(mode="python", exclude={"audit_manifest_digest"}),
            }
        )
        if self.audit_manifest_digest and self.audit_manifest_digest != computed:
            raise ValueError("pilot task audit manifest digest mismatch")
        if not self.audit_manifest_digest:
            object.__setattr__(self, "audit_manifest_digest", computed)
        return self

    @property
    def reserved_task(self) -> AuditedDevelopmentTask | None:
        if not self.reservation_events:
            return None
        digest = self.reservation_events[0].task_manifest_digest
        return next(task for task in self.tasks if task.task_manifest_digest == digest)


def audit_public_development_tasks(
    *,
    audit_id: str,
    epoch: ResearchEpochManifest,
    tasks: Sequence[TaskEnvelope],
    inspected_at_ms: int,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> PilotTaskAuditManifest:
    """Audit canonical public task projections and permanently classify them development."""

    if not tasks:
        raise PilotEvidenceError("pilot task audit requires at least one public task")
    audited: list[AuditedDevelopmentTask] = []
    for sequence, raw_task in enumerate(tasks):
        task = TaskEnvelope.model_validate(raw_task.model_dump(mode="python"))
        assert_task_bound_to_epoch(task, epoch)
        if task.data_state != "development":
            raise PilotEvidenceError("sealed-confirmation tasks may not be opened by pilot audit")
        projection = task_envelope_public_projection(
            task,
            canary_values=canary_values,
            canary_digests=canary_digests,
        )
        _scan_packet_value(
            projection,
            public=True,
            canary_values=canary_values,
            canary_digests=canary_digests,
            allowed_absolute_paths=(task.workspace_snapshot.uri,),
        )
        audited.append(
            AuditedDevelopmentTask(
                audit_sequence=sequence,
                task_manifest_id=task.task_manifest_id,
                task_manifest_digest=task.task_manifest_digest,
                epoch_id=task.epoch_id,
                epoch_manifest_digest=task.epoch_manifest_digest,
                split_manifest_digest=task.split_manifest_digest,
                public_projection=projection,
                inspected_at_ms=inspected_at_ms,
            )
        )
    return PilotTaskAuditManifest(
        audit_id=audit_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        tasks=tuple(audited),
        inspected_task_count=len(audited),
    )


def reserve_audited_pilot_task(
    audit: PilotTaskAuditManifest,
    *,
    pilot_id: str,
    task_manifest_digest: str,
    reserved_at_ms: int,
) -> PilotTaskAuditManifest:
    """Reserve exactly one audited development task; reservations cannot be replaced."""

    audit = PilotTaskAuditManifest.model_validate(audit.model_dump(mode="python"))
    if audit.reservation_state != "none":
        raise PilotEvidenceError("an audit may reserve exactly one pilot task in its lifetime")
    selected = next(
        (task for task in audit.tasks if task.task_manifest_digest == task_manifest_digest),
        None,
    )
    if selected is None:
        raise PilotEvidenceError("only a task in the public audit may be reserved")
    event = PilotReservationEvent(
        sequence=0,
        event="reserved",
        pilot_id=pilot_id,
        task_manifest_id=selected.task_manifest_id,
        task_manifest_digest=selected.task_manifest_digest,
        occurred_at_ms=reserved_at_ms,
    )
    payload = audit.model_dump(mode="python")
    payload.update(
        audit_manifest_digest="",
        reservation_events=(event,),
        reservation_state="reserved",
        reserved_task_count=1,
    )
    return PilotTaskAuditManifest.model_validate(payload)


def consume_reserved_pilot_task(
    audit: PilotTaskAuditManifest,
    *,
    consumption_evidence_digest: str,
    consumed_at_ms: int,
) -> PilotTaskAuditManifest:
    """Record the one pilot use and immediate irreversible development reclassification."""

    audit = PilotTaskAuditManifest.model_validate(audit.model_dump(mode="python"))
    if audit.reservation_state != "reserved":
        raise PilotEvidenceError("pilot task is not in the one consumable reserved state")
    reservation = audit.reservation_events[0]
    digest = _require_digest(consumption_evidence_digest, "consumption_evidence_digest")
    consumed = PilotReservationEvent(
        sequence=1,
        event="consumed",
        pilot_id=reservation.pilot_id,
        task_manifest_id=reservation.task_manifest_id,
        task_manifest_digest=reservation.task_manifest_digest,
        occurred_at_ms=consumed_at_ms,
        consumption_evidence_digest=digest,
    )
    reclassified = PilotReservationEvent(
        sequence=2,
        event="reclassified_development",
        pilot_id=reservation.pilot_id,
        task_manifest_id=reservation.task_manifest_id,
        task_manifest_digest=reservation.task_manifest_digest,
        occurred_at_ms=consumed_at_ms,
        consumption_evidence_digest=digest,
    )
    payload = audit.model_dump(mode="python")
    payload.update(
        audit_manifest_digest="",
        reservation_events=(reservation, consumed, reclassified),
        reservation_state="consumed_reclassified_development",
        consumed_task_count=1,
    )
    return PilotTaskAuditManifest.model_validate(payload)


class PilotModelCall(PilotEvidenceModel):
    sequence: int = Field(ge=0)
    call_id: str
    actor_id: str
    call_kind: Literal["initial", "revision"]
    plan_call_digest: str
    request_digest: str
    context_digest: str
    budget_share_bps: int = Field(gt=0, le=10_000)
    emits_final_patch: bool
    request_sent: Literal[False] = False

    @field_validator("call_id", "actor_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("plan_call_digest", "request_digest", "context_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class PilotToolCall(PilotEvidenceModel):
    sequence: int = Field(ge=0)
    call_id: str
    actor_call_id: str
    tool_id: str
    action_digest: str
    max_output_bytes: int = Field(gt=0)
    call_sent: Literal[False] = False

    @field_validator("call_id", "actor_call_id", "tool_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("action_digest")
    @classmethod
    def validate_action_digest(cls, value: str) -> str:
        return _require_digest(value, "action_digest")


class PilotPublicVerificationCall(PilotEvidenceModel):
    sequence: int = Field(ge=0)
    call_id: str
    step_id: str
    action_digest: str
    timeout_ms: int = Field(gt=0)
    expected_exit_codes: tuple[int, ...] = Field(min_length=1)
    call_sent: Literal[False] = False

    @field_validator("call_id", "step_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("action_digest")
    @classmethod
    def validate_action_digest(cls, value: str) -> str:
        return _require_digest(value, "action_digest")

    @field_validator("expected_exit_codes")
    @classmethod
    def validate_exit_codes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)):
            raise ValueError("planned public verification exit codes may not duplicate")
        return value


class PilotEvaluatorCall(PilotEvidenceModel):
    sequence: int = Field(ge=0)
    call_id: str
    evaluator_id: str
    evaluator_identity_digest: str
    evaluation_contract_digest: str
    action_digest: str
    call_sent: Literal[False] = False

    @field_validator("call_id", "evaluator_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "evaluator_identity_digest",
        "evaluation_contract_digest",
        "action_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


PilotEvidencePurpose = Literal[
    "public_summary",
    "task_audit",
    "pilot_compiled_plan",
    "run_manifest",
    "pre_call_contexts",
    "artifacts",
    "tool_receipts",
    "outcome_receipts",
    "pilot_report",
]

_REQUIRED_PILOT_EVIDENCE_PURPOSES = frozenset(
    {
        "public_summary",
        "task_audit",
        "pilot_compiled_plan",
        "run_manifest",
        "pre_call_contexts",
        "artifacts",
        "tool_receipts",
        "outcome_receipts",
        "pilot_report",
    }
)


class PilotEvidencePath(PilotEvidenceModel):
    purpose: PilotEvidencePurpose
    scope: Literal["public", "controlled"]
    relative_path: str

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value, "pilot evidence path")

    @model_validator(mode="after")
    def validate_scope(self) -> "PilotEvidencePath":
        expected_root = (
            PUBLIC_RELEASE_EVIDENCE_DIR if self.scope == "public" else CONTROLLED_EVIDENCE_DIR
        )
        if PurePosixPath(self.relative_path).parts[0] != expected_root:
            raise ValueError("pilot evidence path crossed its public/controlled scope")
        if self.purpose == "public_summary" and self.scope != "public":
            raise ValueError("pilot public summary must be in public release evidence")
        if self.purpose != "public_summary" and self.scope != "controlled":
            raise ValueError("detailed pilot evidence must remain controlled")
        return self


class PilotBudgetPlan(PilotEvidenceModel):
    ceilings: TaskCeilings
    scheduled_model_calls: int = Field(gt=0)
    scheduled_tool_calls: int = Field(ge=0)
    scheduled_public_verification_calls: int = Field(gt=0)
    scheduled_evaluator_calls: int = Field(gt=0)
    total_runtime_tool_calls: int = Field(gt=0)
    planned_max_known_cost_usd: float = Field(ge=0.0)
    planned_max_estimated_cost_usd: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_budget(self) -> "PilotBudgetPlan":
        if self.total_runtime_tool_calls != (
            self.scheduled_tool_calls + self.scheduled_public_verification_calls
        ):
            raise ValueError("pilot runtime tool-call budget does not exactly cover its call manifest")
        if self.scheduled_model_calls > self.ceilings.max_model_calls:
            raise ValueError("pilot model-call manifest exceeds the frozen task ceiling")
        if self.total_runtime_tool_calls > self.ceilings.max_tool_calls:
            raise ValueError("pilot tool/public manifest exceeds the frozen task ceiling")
        if self.planned_max_known_cost_usd != self.ceilings.max_known_cost_usd:
            raise ValueError("pilot known-cost budget must preserve the frozen task ceiling")
        if self.planned_max_estimated_cost_usd != self.ceilings.max_estimated_cost_usd:
            raise ValueError("pilot estimated-cost budget must preserve the frozen task ceiling")
        return self


def pilot_tool_manifest_digest(plan: CompositeRunPlan) -> str:
    return canonical_identity_digest(
        [
            tool.model_dump(mode="python")
            for tool in plan.dependency_manifest.trusted_tools
        ],
        domain="pilot-trusted-tool-manifest",
    )


def _planned_model_calls(plan: CompositeRunPlan) -> tuple[PilotModelCall, ...]:
    calls: list[PilotModelCall] = []
    for sequence, call in enumerate(plan.actor_calls):
        call_payload = call.model_dump(mode="python", exclude_none=True)
        calls.append(
            PilotModelCall(
                sequence=sequence,
                call_id=call.call_id,
                actor_id=call.actor_id,
                call_kind=call.call_kind,
                plan_call_digest=canonical_identity_digest(
                    call_payload,
                    domain="pilot-actor-call-plan",
                ),
                request_digest=canonical_identity_digest(
                    {
                        "instruction": call.instruction,
                        "revision_instruction": call.revision_instruction,
                        "emits_final_patch": call.emits_final_patch,
                    },
                    domain="pilot-model-request",
                ),
                context_digest=canonical_identity_digest(
                    [read.model_dump(mode="python") for read in call.context_reads],
                    domain="pilot-model-context-plan",
                ),
                budget_share_bps=call.budget_share_bps,
                emits_final_patch=call.emits_final_patch,
            )
        )
    return tuple(calls)


def _planned_public_calls(plan: CompositeRunPlan) -> tuple[PilotPublicVerificationCall, ...]:
    return tuple(
        PilotPublicVerificationCall(
            sequence=sequence,
            call_id=f"public.{action.step_id}",
            step_id=action.step_id,
            action_digest=canonical_identity_digest(
                action.model_dump(mode="python"),
                domain="pilot-public-verification-action",
            ),
            timeout_ms=action.timeout_ms,
            expected_exit_codes=action.expected_exit_codes,
        )
        for sequence, action in enumerate(plan.public_verification.actions)
    )


class PilotDryRunManifest(PilotEvidenceModel):
    schema_version: Literal[PILOT_DRY_RUN_SCHEMA_VERSION] = PILOT_DRY_RUN_SCHEMA_VERSION
    pilot_id: str
    manifest_digest: str = ""
    active_release_digest: str
    release_manifest_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    deployment: DeploymentIdentity
    protocol_source_digest: str
    release_representative_compiled_semantic_digest: str
    pilot_compiled_semantic_digest: str
    dependency_manifest_digest: str
    compiler_digest: str
    kernel_digest: str
    tool_manifest_digest: str
    environment_id: str
    environment_digest: str
    task_manifest_id: str
    task_manifest_digest: str
    task_public_projection_digest: str
    task_audit_manifest_digest: str
    session_id: str
    session_manifest_digest: str
    model_calls: tuple[PilotModelCall, ...] = Field(min_length=1)
    tool_calls: tuple[PilotToolCall, ...]
    public_verification_calls: tuple[PilotPublicVerificationCall, ...] = Field(min_length=1)
    evaluator_calls: tuple[PilotEvaluatorCall, ...] = Field(min_length=1)
    budget: PilotBudgetPlan
    evidence_paths: tuple[PilotEvidencePath, ...]
    non_confirmatory: Literal[True] = True
    live_status: Literal["not_run"] = "not_run"
    inference_requests_sent: Literal[0] = 0
    created_at_ms: int = Field(ge=0)

    @field_validator(
        "pilot_id",
        "epoch_id",
        "environment_id",
        "task_manifest_id",
        "session_id",
    )
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "active_release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "protocol_source_digest",
        "release_representative_compiled_semantic_digest",
        "pilot_compiled_semantic_digest",
        "dependency_manifest_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "environment_digest",
        "task_manifest_digest",
        "task_public_projection_digest",
        "task_audit_manifest_digest",
        "session_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_manifest(self) -> "PilotDryRunManifest":
        call_ids = [
            *(call.call_id for call in self.model_calls),
            *(call.call_id for call in self.tool_calls),
            *(call.call_id for call in self.public_verification_calls),
            *(call.call_id for call in self.evaluator_calls),
        ]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("pilot planned calls must have globally unique identities")
        for calls, label in (
            (self.model_calls, "model"),
            (self.tool_calls, "tool"),
            (self.public_verification_calls, "public verification"),
            (self.evaluator_calls, "evaluator"),
        ):
            if [call.sequence for call in calls] != list(range(len(calls))):
                raise ValueError(f"pilot {label} call schedule must be contiguous and ordered")
        if self.budget.scheduled_model_calls != len(self.model_calls):
            raise ValueError("pilot budget crossed the exact model-call manifest")
        if self.budget.scheduled_tool_calls != len(self.tool_calls):
            raise ValueError("pilot budget crossed the exact tool-call manifest")
        if self.budget.scheduled_public_verification_calls != len(self.public_verification_calls):
            raise ValueError("pilot budget crossed the exact public-call manifest")
        if self.budget.scheduled_evaluator_calls != len(self.evaluator_calls):
            raise ValueError("pilot budget crossed the exact evaluator-call manifest")
        purposes = [path.purpose for path in self.evidence_paths]
        if set(purposes) != _REQUIRED_PILOT_EVIDENCE_PURPOSES or len(purposes) != len(set(purposes)):
            raise ValueError("pilot evidence paths must cover every required purpose exactly once")
        relative_paths = [path.relative_path for path in self.evidence_paths]
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("pilot evidence paths may not duplicate")
        evaluator_identities = {
            (call.evaluator_id, call.evaluator_identity_digest)
            for call in self.evaluator_calls
        }
        if len(evaluator_identities) != 1:
            raise ValueError("pilot evaluator calls must use one frozen evaluator authority")
        computed = evidence_digest(
            {
                "kind": PILOT_DRY_RUN_SCHEMA_VERSION,
                "manifest": self.model_dump(mode="python", exclude={"manifest_digest"}),
            }
        )
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("pilot dry-run manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


def validate_pilot_dry_run_bindings(
    manifest: PilotDryRunManifest,
    *,
    active_release: ActiveReleasePointer,
    release_manifest: HarnessReleaseManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    plan: CompositeRunPlan,
    audit: PilotTaskAuditManifest,
    session_release_digest: str,
) -> None:
    manifest = PilotDryRunManifest.model_validate(manifest.model_dump(mode="python"))
    assert_task_bound_to_epoch(task, epoch)
    if task.data_state != "development":
        raise PilotEvidenceError("pilot dry run may only bind a development task")
    if audit.reservation_state != "reserved" or audit.reserved_task is None:
        raise PilotEvidenceError("pilot dry run requires the one audited task to remain reserved")
    if audit.reserved_task.task_manifest_digest != task.task_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed the reserved public task")
    if audit.reservation_events[0].pilot_id != manifest.pilot_id:
        raise PilotEvidenceError("pilot dry run crossed reservation identity")
    if active_release.release_digest != release_manifest.release_digest:
        raise PilotEvidenceError("active release pointer crossed immutable release content")
    if active_release.manifest_digest != release_manifest.manifest_digest:
        raise PilotEvidenceError("active release pointer crossed release manifest identity")
    expected = {
        "active_release_digest": release_manifest.release_digest,
        "release_manifest_digest": release_manifest.manifest_digest,
        "epoch_id": epoch.epoch_id,
        "epoch_manifest_digest": epoch.epoch_manifest_digest,
        "protocol_source_digest": plan.source_protocol_digest,
        "release_representative_compiled_semantic_digest": (
            release_manifest.compiled_semantic_digest
        ),
        "pilot_compiled_semantic_digest": plan.compiled_semantic_digest,
        "dependency_manifest_digest": plan.dependency_manifest_digest,
        "compiler_digest": plan.dependency_manifest.compiler.implementation_digest,
        "kernel_digest": plan.dependency_manifest.kernel.implementation_digest,
        "tool_manifest_digest": pilot_tool_manifest_digest(plan),
        "task_manifest_id": task.task_manifest_id,
        "task_manifest_digest": task.task_manifest_digest,
        "task_public_projection_digest": audit.reserved_task.public_projection_digest,
        "task_audit_manifest_digest": audit.audit_manifest_digest,
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(manifest, field_name) != expected_value
    ]
    if crossed:
        raise PilotEvidenceError("pilot dry run crossed frozen identity: " + ", ".join(crossed))
    if manifest.deployment != epoch.deployment or release_manifest.deployment != epoch.deployment:
        raise PilotEvidenceError("pilot dry run crossed the active deployment")
    if session_release_digest != manifest.active_release_digest:
        raise PilotEvidenceError("pilot session is not pinned to the active immutable release")
    if plan.task_envelope_digest != task.task_manifest_digest:
        raise PilotEvidenceError("pilot compiled plan crossed the reserved task")
    if release_manifest.epoch_manifest_digest != epoch.epoch_manifest_digest:
        raise PilotEvidenceError("active release crossed the pilot epoch")
    if release_manifest.protocol_source_digest != plan.source_protocol_digest:
        raise PilotEvidenceError("active release crossed the pilot protocol")
    if release_manifest.dependency_manifest_digest != plan.dependency_manifest_digest:
        raise PilotEvidenceError("active release crossed pilot runtime dependencies")
    if manifest.model_calls != _planned_model_calls(plan):
        raise PilotEvidenceError("pilot model-call manifest is not the exact compiled schedule")
    if manifest.public_verification_calls != _planned_public_calls(plan):
        raise PilotEvidenceError("pilot public-call manifest is not the exact compiled verification plan")
    planned_call_tools = {call.call_id: set(call.tool_ids) for call in plan.actor_calls}
    dependency_tools = {tool.tool_id for tool in plan.dependency_manifest.trusted_tools}
    for call in manifest.tool_calls:
        if call.actor_call_id not in planned_call_tools:
            raise PilotEvidenceError("pilot tool call references an unknown actor call")
        if call.tool_id not in planned_call_tools[call.actor_call_id]:
            raise PilotEvidenceError("pilot tool call is outside its actor tool authority")
        if call.tool_id not in dependency_tools:
            raise PilotEvidenceError("pilot tool call is outside the frozen dependency manifest")
    if manifest.budget.ceilings != plan.budget_ledger.aggregate_ceiling:
        raise PilotEvidenceError("pilot budget crossed the compiled aggregate ceiling")


def build_pilot_dry_run_manifest(
    *,
    pilot_id: str,
    active_release: ActiveReleasePointer,
    release_manifest: HarnessReleaseManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    plan: CompositeRunPlan,
    audit: PilotTaskAuditManifest,
    session_id: str,
    session_manifest_digest: str,
    session_release_digest: str,
    environment_id: str,
    environment_digest: str,
    tool_calls: Sequence[PilotToolCall],
    evaluator_calls: Sequence[PilotEvaluatorCall],
    evidence_paths: Sequence[PilotEvidencePath],
    created_at_ms: int,
) -> PilotDryRunManifest:
    """Build a request-free, exact-call pilot manifest bound to the active release."""

    manifest = PilotDryRunManifest(
        pilot_id=pilot_id,
        active_release_digest=active_release.release_digest,
        release_manifest_digest=active_release.manifest_digest,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        deployment=epoch.deployment,
        protocol_source_digest=plan.source_protocol_digest,
        release_representative_compiled_semantic_digest=(
            release_manifest.compiled_semantic_digest
        ),
        pilot_compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=plan.dependency_manifest_digest,
        compiler_digest=plan.dependency_manifest.compiler.implementation_digest,
        kernel_digest=plan.dependency_manifest.kernel.implementation_digest,
        tool_manifest_digest=pilot_tool_manifest_digest(plan),
        environment_id=environment_id,
        environment_digest=environment_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        task_public_projection_digest=(
            audit.reserved_task.public_projection_digest if audit.reserved_task else ""
        ),
        task_audit_manifest_digest=audit.audit_manifest_digest,
        session_id=session_id,
        session_manifest_digest=session_manifest_digest,
        model_calls=_planned_model_calls(plan),
        tool_calls=tuple(tool_calls),
        public_verification_calls=_planned_public_calls(plan),
        evaluator_calls=tuple(evaluator_calls),
        budget=PilotBudgetPlan(
            ceilings=plan.budget_ledger.aggregate_ceiling,
            scheduled_model_calls=len(plan.actor_calls),
            scheduled_tool_calls=len(tool_calls),
            scheduled_public_verification_calls=len(plan.public_verification.actions),
            scheduled_evaluator_calls=len(evaluator_calls),
            total_runtime_tool_calls=len(tool_calls) + len(plan.public_verification.actions),
            planned_max_known_cost_usd=plan.budget_ledger.aggregate_ceiling.max_known_cost_usd,
            planned_max_estimated_cost_usd=plan.budget_ledger.aggregate_ceiling.max_estimated_cost_usd,
        ),
        evidence_paths=tuple(evidence_paths),
        created_at_ms=created_at_ms,
    )
    validate_pilot_dry_run_bindings(
        manifest,
        active_release=active_release,
        release_manifest=release_manifest,
        epoch=epoch,
        task=task,
        plan=plan,
        audit=audit,
        session_release_digest=session_release_digest,
    )
    return manifest


class PilotLiveExecutionAuthorization(PilotEvidenceModel):
    pilot_manifest_digest: str
    task_audit_manifest_digest: str
    live_authorized: Literal[True] = True
    deployment: DeploymentIdentity
    deployment_profile: HarnessDeploymentProfile
    profile_digest: str
    credential_reference: CredentialReference
    credential_reference_digest: str

    @field_validator(
        "pilot_manifest_digest",
        "task_audit_manifest_digest",
        "profile_digest",
        "credential_reference_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_frozen_live_authority(self) -> "PilotLiveExecutionAuthorization":
        profile = self.deployment_profile
        try:
            profile.validate_deployment_identity(self.deployment)
        except ValueError as exc:
            raise ValueError("pilot live authorization crossed its frozen deployment profile") from exc
        if self.profile_digest != harness_deployment_profile_digest(profile):
            raise ValueError("pilot live authorization profile digest mismatch")
        if self.credential_reference.provider_name != profile.provider:
            raise ValueError("pilot credential reference crossed the frozen provider")
        if (
            self.credential_reference.api_key_env != profile.endpoint.api_key_env
            or self.credential_reference.api_key_file_env
            != profile.endpoint.api_key_file_env
        ):
            raise ValueError("pilot credential reference differs from the frozen endpoint policy")
        if self.credential_reference_digest != _credential_reference_digest(
            self.credential_reference
        ):
            raise ValueError("pilot credential-reference digest mismatch")
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


def _credential_reference_digest(value: CredentialReference) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-credential-reference-v1",
            **value.model_dump(mode="python", exclude_none=True),
        }
    )


def require_pilot_live_authorization(
    manifest: PilotDryRunManifest,
    audit: PilotTaskAuditManifest,
    *,
    deployment_profile: HarnessDeploymentProfile,
    live_authorized: bool,
    credential_reference: CredentialReference | None,
) -> PilotLiveExecutionAuthorization:
    """Validate authority only. This module deliberately exposes no live executor."""

    if not live_authorized:
        raise PilotEvidenceError("pilot live execution requires explicit authorization")
    if audit.reservation_state != "reserved":
        raise PilotEvidenceError("pilot live authorization requires an unconsumed reservation")
    if manifest.task_audit_manifest_digest != audit.audit_manifest_digest:
        raise PilotEvidenceError("pilot live authorization crossed the task audit")
    if not credential_reference:
        raise PilotEvidenceError("pilot live authorization requires a credential reference")
    try:
        profile_payload = (
            deployment_profile.model_dump(mode="python")
            if isinstance(deployment_profile, HarnessDeploymentProfile)
            else deployment_profile
        )
        credential_payload = (
            credential_reference.model_dump(mode="python")
            if isinstance(credential_reference, CredentialReference)
            else credential_reference
        )
        profile = HarnessDeploymentProfile.model_validate(profile_payload)
        profile.validate_deployment_identity(manifest.deployment)
        credential = CredentialReference.model_validate(credential_payload)
        return PilotLiveExecutionAuthorization(
            pilot_manifest_digest=manifest.manifest_digest,
            task_audit_manifest_digest=audit.audit_manifest_digest,
            deployment=manifest.deployment,
            deployment_profile=profile,
            profile_digest=harness_deployment_profile_digest(profile),
            credential_reference=credential,
            credential_reference_digest=_credential_reference_digest(credential),
        )
    except (TypeError, ValueError) as exc:
        raise PilotEvidenceError(str(exc)) from exc


class ImmutableReleaseIdentity(PilotEvidenceModel):
    release_digest: str
    release_manifest_digest: str
    release_path: str
    epoch_id: str
    epoch_manifest_digest: str
    deployment: DeploymentIdentity
    protocol_source_digest: str
    representative_compiled_semantic_digest: str
    dependency_manifest_digest: str
    profile_digest: str
    immutable: Literal[True] = True

    @field_validator("epoch_id")
    @classmethod
    def validate_epoch_id(cls, value: str) -> str:
        return _require_identifier(value, "epoch_id")

    @field_validator("release_path")
    @classmethod
    def validate_release_path(cls, value: str) -> str:
        return _relative_path(value, "release_path")

    @field_validator(
        "release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "protocol_source_digest",
        "representative_compiled_semantic_digest",
        "dependency_manifest_digest",
        "profile_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @classmethod
    def from_records(
        cls,
        pointer: ActiveReleasePointer,
        manifest: HarnessReleaseManifest,
    ) -> "ImmutableReleaseIdentity":
        if pointer.release_digest != manifest.release_digest:
            raise PilotEvidenceError("active release pointer crossed immutable release digest")
        if pointer.manifest_digest != manifest.manifest_digest:
            raise PilotEvidenceError("active release pointer crossed release manifest digest")
        expected_path = f"releases/{manifest.release_digest}"
        if pointer.release_path != expected_path:
            raise PilotEvidenceError("active release pointer is not content-addressed")
        return cls(
            release_digest=manifest.release_digest,
            release_manifest_digest=manifest.manifest_digest,
            release_path=pointer.release_path,
            epoch_id=manifest.epoch_id,
            epoch_manifest_digest=manifest.epoch_manifest_digest,
            deployment=manifest.deployment,
            protocol_source_digest=manifest.protocol_source_digest,
            representative_compiled_semantic_digest=manifest.compiled_semantic_digest,
            dependency_manifest_digest=manifest.dependency_manifest_digest,
            profile_digest=manifest.profile_digest,
        )


def gate0_conformance_report_digest(report: Gate0ConformanceReport) -> str:
    return evidence_digest(
        {
            "kind": report.schema_version,
            "report": report.model_dump(mode="python"),
        }
    )


class Gate0OfflineReadiness(PilotEvidenceModel):
    dry_run_manifest_digest: str
    panel_digest: str
    preregistration_digest: str
    deterministic_suite_digest: str
    deterministic_conformance_report_digest: str
    deterministic_conformance_passed: Literal[True] = True
    planned_provider_calls: int = Field(gt=0)
    live_status: Literal["not_run"] = "not_run"
    inference_requests_sent: Literal[0] = 0

    @field_validator(
        "dry_run_manifest_digest",
        "panel_digest",
        "preregistration_digest",
        "deterministic_suite_digest",
        "deterministic_conformance_report_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @classmethod
    def from_records(
        cls,
        *,
        manifest: Gate0DryRunManifest,
        conformance: Gate0ConformanceReport,
        preregistration: Gate0PreregistrationPublic,
        live_report: Gate0NotRunReport,
    ) -> "Gate0OfflineReadiness":
        if manifest.live_status != "not_run":
            raise PilotEvidenceError("Gate0 dry run must not claim live execution")
        if conformance.live_status != "not_run" or not conformance.passed:
            raise PilotEvidenceError("Gate0 deterministic conformance must pass without live execution")
        if conformance.manifest_digest != manifest.manifest_digest:
            raise PilotEvidenceError("Gate0 conformance crossed its dry-run manifest")
        if preregistration.panel_digest != manifest.panel.panel_digest:
            raise PilotEvidenceError("public Gate0 preregistration crossed the frozen panel")
        if preregistration.planned_provider_calls != manifest.total_provider_calls:
            raise PilotEvidenceError("public Gate0 preregistration crossed exact planned calls")
        if live_report.status != "not_run":
            raise PilotEvidenceError("Gate0 live report must remain not_run")
        if live_report.preregistration_digest != preregistration.preregistration_digest:
            raise PilotEvidenceError("Gate0 live report crossed preregistration")
        report_digest = gate0_conformance_report_digest(conformance)
        if preregistration.deterministic_suite_digest != report_digest:
            raise PilotEvidenceError("Gate0 preregistration crossed deterministic conformance evidence")
        return cls(
            dry_run_manifest_digest=manifest.manifest_digest,
            panel_digest=manifest.panel.panel_digest,
            preregistration_digest=preregistration.preregistration_digest,
            deterministic_suite_digest=preregistration.deterministic_suite_digest,
            deterministic_conformance_report_digest=report_digest,
            planned_provider_calls=manifest.total_provider_calls,
        )


class D0FixtureFeasibilityEvidence(PilotEvidenceModel):
    feasibility_manifest_digest: str
    task_manifest_digest: str
    evaluation_contract_digest: str
    execution_backend_digest: str
    offline_controls_passed: Literal[True] = True
    clean_replay_reproducible: Literal[True] = True
    protected_path_integrity: Literal[True] = True
    leakage_integrity: Literal[True] = True
    identity_integrity: Literal[True] = True
    feasibility_status: Literal["pass", "pending_real_provider_baseline"]
    real_provider_baseline_status: Literal["not_run"] = "not_run"
    inference_requests_sent: Literal[0] = 0

    @field_validator(
        "feasibility_manifest_digest",
        "task_manifest_digest",
        "evaluation_contract_digest",
        "execution_backend_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @classmethod
    def from_manifest(
        cls,
        manifest: DevelopmentTaskFeasibilityManifest,
    ) -> "D0FixtureFeasibilityEvidence":
        if manifest.status == "fail":
            raise PilotEvidenceError("failing D0 fixture evidence cannot authorize implementation readiness")
        required = {
            "offline_controls_passed": manifest.offline_controls_passed,
            "clean_replay_reproducible": manifest.clean_replay_reproducible,
            "protected_path_integrity": manifest.protected_path_integrity,
            "leakage_integrity": manifest.leakage_integrity,
            "identity_integrity": manifest.identity_integrity,
        }
        failed = [name for name, passed in required.items() if not passed]
        if failed:
            raise PilotEvidenceError("D0 fixture evidence failed: " + ", ".join(failed))
        if manifest.provider_baseline_dry_run.real_provider_baseline_status != "not_run":
            raise PilotEvidenceError("D0 real-provider baseline must remain not_run")
        return cls(
            feasibility_manifest_digest=manifest.manifest_digest,
            task_manifest_digest=manifest.task_manifest_digest,
            evaluation_contract_digest=manifest.evaluation_contract_digest,
            execution_backend_digest=manifest.execution_backend_digest,
            feasibility_status=manifest.status,
        )


class RetainedStructuralTransactionEvidence(PilotEvidenceModel):
    transaction_id: str
    transaction_digest: str
    operator: Literal[
        "actor_split",
        "channel_add",
        "channel_rewire",
        "revision_insert",
        "revision_remove",
    ]
    treatment_class: Literal["structural"] = "structural"
    parent_protocol_digest: str
    child_protocol_digest: str

    @field_validator("transaction_id")
    @classmethod
    def validate_transaction_id(cls, value: str) -> str:
        return _require_identifier(value, "transaction_id")

    @field_validator(
        "transaction_digest",
        "parent_protocol_digest",
        "child_protocol_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class S1OfflineRetentionEvidence(PilotEvidenceModel):
    search_result_digest: str
    epoch_manifest_digest: str
    task_panel_digest: str
    founding_protocol_digest: str
    final_protocol_digest: str
    execution_mode: Literal["offline_scripted"] = "offline_scripted"
    execution_status: Literal["completed", "stopped_by_rule"]
    feasibility_status: Literal["search_viable"] = "search_viable"
    retained_children: int = Field(gt=0)
    paired_external_outcomes_verified: Literal[True] = True
    retained_structural_transactions: tuple[RetainedStructuralTransactionEvidence, ...] = Field(
        min_length=1
    )
    live_inference_status: Literal["not_run"] = "not_run"
    inference_requests_sent: Literal[0] = 0

    @field_validator(
        "search_result_digest",
        "epoch_manifest_digest",
        "task_panel_digest",
        "founding_protocol_digest",
        "final_protocol_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_retention(self) -> "S1OfflineRetentionEvidence":
        if self.retained_children < len(self.retained_structural_transactions):
            raise ValueError("S1 structural retention exceeds recorded retained children")
        digests = [item.transaction_digest for item in self.retained_structural_transactions]
        if len(digests) != len(set(digests)):
            raise ValueError("S1 retained structural transaction evidence may not duplicate")
        return self

    @classmethod
    def from_result(cls, result: PairedHarnessSearchResult) -> "S1OfflineRetentionEvidence":
        if result.execution_mode != "offline_scripted":
            raise PilotEvidenceError("S1 readiness requires the offline-scripted execution lane")
        if result.final_status.live_inference_status != "not_run":
            raise PilotEvidenceError("S1 live inference status must remain not_run")
        if result.final_status.inference_requests_sent != 0:
            raise PilotEvidenceError("S1 no-live evidence cannot contain inference requests")
        if result.final_status.execution_status not in {"completed", "stopped_by_rule"}:
            raise PilotEvidenceError("S1 did not complete a viable offline search")
        if result.final_status.feasibility_status != "search_viable":
            raise PilotEvidenceError("S1 did not retain an outcome-improving descendant")
        structural = tuple(
            RetainedStructuralTransactionEvidence(
                transaction_id=transaction.transaction_id,
                transaction_digest=transaction.transaction_record_digest,
                operator=transaction.operator,
                treatment_class=transaction.treatment_class,
                parent_protocol_digest=transaction.parent_source_protocol_digest,
                child_protocol_digest=transaction.child_source_protocol_digest,
            )
            for transaction in result.retained_transactions
            if transaction.treatment_class == "structural"
            and transaction.operator != "instruction_rewrite"
        )
        if not structural or result.retained_children < 1:
            raise PilotEvidenceError("S1 readiness requires a retained non-prompt semantic child")
        return cls(
            search_result_digest=result.result_digest,
            epoch_manifest_digest=result.epoch_manifest_digest,
            task_panel_digest=result.task_panel_digest,
            founding_protocol_digest=result.founding_protocol.source_digest(),
            final_protocol_digest=result.final_protocol.source_digest(),
            execution_status=result.final_status.execution_status,
            retained_children=result.retained_children,
            retained_structural_transactions=structural,
        )

    @classmethod
    def from_factory_records(
        cls,
        *,
        result: HarnessFactoryBuildResult,
        epoch: ResearchEpochManifest,
        founding_protocol_digest: str,
        final_protocol_digest: str,
        lineage: Sequence[PublicSearchLineageRecord],
    ) -> "S1OfflineRetentionEvidence":
        if result.execution_mode != "offline_scripted" or result.live_status != "not_run":
            raise PilotEvidenceError("S1 readiness requires an offline, no-live factory build")
        if result.search_execution_status not in {"completed", "stopped_by_rule"}:
            raise PilotEvidenceError("factory S1 did not complete a viable offline search")
        if result.search_feasibility_status != "search_viable":
            raise PilotEvidenceError("factory S1 did not retain an outcome-improving descendant")
        if result.selected_protocol_digest != final_protocol_digest:
            raise PilotEvidenceError("factory S1 selected protocol crossed the immutable release")
        structural = tuple(
            RetainedStructuralTransactionEvidence(
                transaction_id=record.transaction_id,
                transaction_digest=record.transaction_digest,
                operator=record.operator,
                parent_protocol_digest=record.parent_protocol_digest,
                child_protocol_digest=record.child_protocol_digest,
            )
            for record in lineage
            if record.status == "accepted" and record.operator != "instruction_rewrite"
        )
        if not structural:
            raise PilotEvidenceError("factory S1 readiness requires retained non-prompt lineage")
        if structural[-1].child_protocol_digest != final_protocol_digest:
            raise PilotEvidenceError("factory S1 structural lineage does not reach the selected protocol")
        return cls(
            search_result_digest=result.search_result_digest,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            task_panel_digest=epoch.search_envelope.task_panel_digest,
            founding_protocol_digest=founding_protocol_digest,
            final_protocol_digest=final_protocol_digest,
            execution_status=result.search_execution_status,
            retained_children=len(structural),
            retained_structural_transactions=structural,
        )


class FactoryFollowupIdentityEvidence(PilotEvidenceModel):
    chat_id: str
    chat_digest: str
    message_id: str
    message_digest: str
    message_index: int = Field(gt=0)
    transaction_id: str
    transaction_digest: str
    prior_release_digest: str
    new_release_digest: str
    new_manifest_digest: str
    new_protocol_digest: str
    representative_compiled_semantic_digest: str
    dependency_manifest_digest: str
    epoch_manifest_digest: str

    @field_validator("chat_id", "message_id", "transaction_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "chat_digest",
        "message_digest",
        "transaction_digest",
        "prior_release_digest",
        "new_release_digest",
        "new_manifest_digest",
        "new_protocol_digest",
        "representative_compiled_semantic_digest",
        "dependency_manifest_digest",
        "epoch_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_followup(self) -> "FactoryFollowupIdentityEvidence":
        if self.prior_release_digest == self.new_release_digest:
            raise ValueError("factory follow-up must produce a distinct immutable release")
        return self

    @classmethod
    def from_records(
        cls,
        *,
        chat: HarnessFactoryChatManifest,
        message: HarnessFactoryMessage,
    ) -> "FactoryFollowupIdentityEvidence":
        if message.message_index <= 0 or message.prior_active_release_digest is None:
            raise PilotEvidenceError("F1b evidence requires a serial factory follow-up")
        if chat.chat_id != message.chat_id or chat.last_message_id != message.message_id:
            raise PilotEvidenceError("factory chat crossed its visible follow-up message")
        if chat.active_release_digest != message.new_release_digest:
            raise PilotEvidenceError("factory chat crossed follow-up release identity")
        if chat.active_manifest_digest != message.new_manifest_digest:
            raise PilotEvidenceError("factory chat crossed follow-up manifest identity")
        if chat.active_protocol_digest != message.new_protocol_digest:
            raise PilotEvidenceError("factory chat crossed follow-up protocol identity")
        if chat.epoch_manifest_digest != message.epoch_manifest_digest:
            raise PilotEvidenceError("factory chat crossed follow-up epoch identity")
        return cls(
            chat_id=chat.chat_id,
            chat_digest=chat.chat_digest,
            message_id=message.message_id,
            message_digest=message.message_digest,
            message_index=message.message_index,
            transaction_id=message.transaction_id,
            transaction_digest=message.transaction_digest,
            prior_release_digest=message.prior_active_release_digest,
            new_release_digest=message.new_release_digest,
            new_manifest_digest=message.new_manifest_digest,
            new_protocol_digest=message.new_protocol_digest,
            representative_compiled_semantic_digest=message.compiled_semantic_digest,
            dependency_manifest_digest=message.dependency_manifest_digest,
            epoch_manifest_digest=message.epoch_manifest_digest,
        )


class RuntimeSessionIdentityEvidence(PilotEvidenceModel):
    role: Literal["same_release_continuation", "independent_new_session"]
    session_id: str
    session_manifest_digest: str
    active_release_digest: str
    message_count: int = Field(ge=0)
    last_message_id: str | None = None
    bounded_public_carryover_count: int = Field(ge=0)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return _require_identifier(value, "session_id")

    @field_validator("session_manifest_digest", "active_release_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("last_message_id")
    @classmethod
    def validate_optional_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value, "last_message_id")

    @model_validator(mode="after")
    def validate_role(self) -> "RuntimeSessionIdentityEvidence":
        if self.role == "same_release_continuation" and self.message_count < 1:
            raise ValueError("same-release continuation evidence requires a completed message")
        if self.role == "independent_new_session" and (
            self.message_count != 0
            or self.last_message_id is not None
            or self.bounded_public_carryover_count != 0
        ):
            raise ValueError("independent new-session evidence must begin without inherited history")
        return self

    @classmethod
    def from_manifest(
        cls,
        manifest: HarnessSessionManifest,
        *,
        role: Literal["same_release_continuation", "independent_new_session"],
    ) -> "RuntimeSessionIdentityEvidence":
        return cls(
            role=role,
            session_id=manifest.session_id,
            session_manifest_digest=manifest.manifest_digest,
            active_release_digest=manifest.active_release_digest,
            message_count=manifest.message_count,
            last_message_id=manifest.last_message_id,
            bounded_public_carryover_count=len(manifest.carryover),
        )


class RuntimeSessionIdentitySet(PilotEvidenceModel):
    sessions: tuple[RuntimeSessionIdentityEvidence, ...] = Field(min_length=2, max_length=2)
    evidence_digest: str = ""

    @model_validator(mode="after")
    def validate_sessions(self) -> "RuntimeSessionIdentitySet":
        roles = tuple(session.role for session in self.sessions)
        if set(roles) != {"same_release_continuation", "independent_new_session"}:
            raise ValueError("F1c evidence requires continuation and independent-new session identities")
        ids = [session.session_id for session in self.sessions]
        if len(ids) != len(set(ids)):
            raise ValueError("continued and new runtime sessions must have distinct identities")
        releases = {session.active_release_digest for session in self.sessions}
        if len(releases) != 1:
            raise ValueError("F1c session evidence must be pinned to one immutable release")
        computed = evidence_digest(
            {
                "kind": "runtime-session-identity-set-v1",
                "sessions": [session.model_dump(mode="python") for session in self.sessions],
            }
        )
        if self.evidence_digest and self.evidence_digest != computed:
            raise ValueError("runtime session identity evidence digest mismatch")
        if not self.evidence_digest:
            object.__setattr__(self, "evidence_digest", computed)
        return self


class PilotNotRunDevelopmentReport(PilotEvidenceModel):
    schema_version: Literal[PILOT_NOT_RUN_REPORT_SCHEMA_VERSION] = (
        PILOT_NOT_RUN_REPORT_SCHEMA_VERSION
    )
    report_digest: str = ""
    pilot_id: str
    pilot_manifest_digest: str
    task_audit_manifest_digest: str
    reserved_task_manifest_digest: str
    status: Literal["not_run"] = "not_run"
    non_confirmatory: Literal[True] = True
    inference_requests_sent: Literal[0] = 0
    capability_claim_authorized: Literal[False] = False
    reason: Literal["real_inference_not_authorized"] = "real_inference_not_authorized"

    @field_validator("pilot_id")
    @classmethod
    def validate_pilot_id(cls, value: str) -> str:
        return _require_identifier(value, "pilot_id")

    @field_validator(
        "pilot_manifest_digest",
        "task_audit_manifest_digest",
        "reserved_task_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_report(self) -> "PilotNotRunDevelopmentReport":
        computed = evidence_digest(
            {
                "kind": PILOT_NOT_RUN_REPORT_SCHEMA_VERSION,
                "report": self.model_dump(mode="python", exclude={"report_digest"}),
            }
        )
        if self.report_digest and self.report_digest != computed:
            raise ValueError("pilot not-run report digest mismatch")
        if not self.report_digest:
            object.__setattr__(self, "report_digest", computed)
        return self


class OfflineSolveExecutionProvenance(PilotEvidenceModel):
    provenance_id: str
    provenance_digest: str = ""
    execution_mode: Literal["deterministic_scripted_replay"] = (
        "deterministic_scripted_replay"
    )
    task_manifest_digest: str
    protocol_source_digest: str
    pilot_compiled_semantic_digest: str
    environment_digest: str
    replay_fixture_digest: str
    solve_result_digest: str
    deterministic_replay_verified: Literal[True] = True
    live_inference_status: Literal["not_run"] = "not_run"
    real_inference_requests_sent: Literal[0] = 0
    provider_invocation_receipt_digests: tuple[str, ...] = ()

    @field_validator("provenance_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_identifier(value, "provenance_id")

    @field_validator(
        "task_manifest_digest",
        "protocol_source_digest",
        "pilot_compiled_semantic_digest",
        "environment_digest",
        "replay_fixture_digest",
        "solve_result_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("provider_invocation_receipt_digests")
    @classmethod
    def reject_provider_receipts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError(
                "offline solve provenance cannot contain provider invocation receipts"
            )
        return value

    @model_validator(mode="after")
    def bind_provenance(self) -> "OfflineSolveExecutionProvenance":
        computed = evidence_digest(
            {
                "kind": "offline-solve-execution-provenance-v1",
                "provenance": self.model_dump(
                    mode="python",
                    exclude={"provenance_digest"},
                ),
            }
        )
        if self.provenance_digest and self.provenance_digest != computed:
            raise ValueError("offline solve execution provenance digest mismatch")
        if not self.provenance_digest:
            object.__setattr__(self, "provenance_digest", computed)
        return self


class PilotEvaluationContractEvidence(PilotEvidenceModel):
    schema_version: Literal[PILOT_EVALUATION_CONTRACT_SCHEMA_VERSION] = (
        PILOT_EVALUATION_CONTRACT_SCHEMA_VERSION
    )
    evaluation_contract_id: str
    evaluation_contract_digest: str
    evaluator_id: str
    evaluator_identity_digest: str
    evaluation_policy_digest: str
    public_projection_digest: str = ""

    @field_validator("evaluation_contract_id", "evaluator_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "evaluation_contract_digest",
        "evaluator_identity_digest",
        "evaluation_policy_digest",
        "public_projection_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "public_projection_digest" and not value:
            return ""
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_public_projection_digest(self) -> "PilotEvaluationContractEvidence":
        computed = evidence_digest(
            {
                "kind": PILOT_EVALUATION_CONTRACT_SCHEMA_VERSION,
                "public_projection": self.model_dump(
                    mode="python",
                    exclude={"public_projection_digest"},
                ),
            }
        )
        if self.public_projection_digest and self.public_projection_digest != computed:
            raise ValueError("pilot evaluation contract public projection digest mismatch")
        if not self.public_projection_digest:
            object.__setattr__(self, "public_projection_digest", computed)
        return self

    @classmethod
    def from_contract(cls, contract: Any) -> "PilotEvaluationContractEvidence":
        """Project evaluator-owned authority without exposing sealed contract fields."""

        authority = contract.outcome_authority
        return cls(
            evaluation_contract_id=contract.evaluation_contract_id,
            evaluation_contract_digest=contract.evaluation_contract_digest,
            evaluator_id=authority.evaluator_id,
            evaluator_identity_digest=authority.evaluator_identity_digest,
            evaluation_policy_digest=authority.evaluation_policy_digest,
        )


class PilotRawPairedOutcomeRecord(PilotEvidenceModel):
    schema_version: Literal[PILOT_RAW_PAIRED_OUTCOME_SCHEMA_VERSION] = (
        PILOT_RAW_PAIRED_OUTCOME_SCHEMA_VERSION
    )
    pair_key_digest: str
    parent_receipt_digest: str
    child_receipt_digest: str
    parent_complete_repair: bool
    child_complete_repair: bool
    non_confirmatory: Literal[True] = True

    @field_validator("pair_key_digest", "parent_receipt_digest", "child_receipt_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class PilotContentNullInterventionEvidence(PilotEvidenceModel):
    schema_version: Literal[PILOT_INTERVENTION_SCHEMA_VERSION] = (
        PILOT_INTERVENTION_SCHEMA_VERSION
    )
    intervention_id: str
    manifest_digest: str = ""
    intervention_kind: Literal["content_null"] = "content_null"
    status: Literal["offline_conformant"] = "offline_conformant"
    paired_outcome_digest: str
    neutral_artifact_digest: str

    @field_validator("intervention_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _require_identifier(value, "intervention_id")

    @field_validator("manifest_digest", "paired_outcome_digest", "neutral_artifact_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "manifest_digest" and not value:
            return ""
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_manifest_digest(self) -> "PilotContentNullInterventionEvidence":
        computed = evidence_digest(
            {
                "kind": PILOT_INTERVENTION_SCHEMA_VERSION,
                "intervention": self.model_dump(
                    mode="python",
                    exclude={"manifest_digest"},
                ),
            }
        )
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("pilot intervention manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


class PilotGateDeterministicTestEvidence(PilotEvidenceModel):
    schema_version: Literal[PILOT_GATE_TEST_EVIDENCE_SCHEMA_VERSION] = (
        PILOT_GATE_TEST_EVIDENCE_SCHEMA_VERSION
    )
    gate_id: str
    test_id: str
    test_command: str
    test_result_digest: str
    live_inference_used: Literal[False] = False
    status: Literal["passed"] = "passed"

    @field_validator("gate_id")
    @classmethod
    def validate_gate_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in REQUIRED_MVP_GATES:
            raise ValueError(f"unknown MVP gate {normalized!r}")
        return normalized

    @field_validator("test_id", "test_command")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{info.field_name} may not be empty")
        return normalized

    @field_validator("test_result_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "test_result_digest")


class GateEvidenceReference(PilotEvidenceModel):
    gate_id: str
    evidence_path: str
    evidence_digest: str
    evidence_kind: Literal["offline_conformance", "deterministic_test", "immutable_identity"]

    @field_validator("gate_id")
    @classmethod
    def validate_gate_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in REQUIRED_MVP_GATES:
            raise ValueError(f"unknown MVP gate {normalized!r}")
        return normalized

    @field_validator("evidence_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = _relative_path(value, "gate evidence path")
        if PurePosixPath(normalized).parts[0] != CONTROLLED_EVIDENCE_DIR:
            raise ValueError("gate implementation evidence must remain controlled")
        return normalized

    @field_validator("evidence_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "evidence_digest")


class GateImplementationEvidence(PilotEvidenceModel):
    gate_id: str
    status: Literal["passed"] = "passed"
    evidence_kind: Literal["offline_conformance", "deterministic_test", "immutable_identity"]
    result_digest: str
    backing_artifact_path: str
    backing_artifact_digest: str
    backing_artifact_schema: Literal[
        "gate0_conformance",
        "d0_feasibility",
        "s1_retention",
        "factory_followup_message",
        "runtime_session_identity_set",
        "deterministic_test_evidence",
    ]
    live_inference_used: Literal[False] = False

    @field_validator("gate_id")
    @classmethod
    def validate_gate_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in REQUIRED_MVP_GATES:
            raise ValueError(f"unknown MVP gate {normalized!r}")
        return normalized

    @field_validator("result_digest")
    @classmethod
    def validate_result_digest(cls, value: str) -> str:
        return _require_digest(value, "result_digest")

    @field_validator("backing_artifact_digest")
    @classmethod
    def validate_backing_digest(cls, value: str) -> str:
        return _require_digest(value, "backing_artifact_digest")

    @field_validator("backing_artifact_path")
    @classmethod
    def validate_backing_path(cls, value: str) -> str:
        normalized = _relative_path(value, "gate backing artifact path")
        if PurePosixPath(normalized).parts[0] != CONTROLLED_EVIDENCE_DIR:
            raise ValueError("gate backing artifact must remain controlled")
        return normalized

    @model_validator(mode="after")
    def validate_backing_digest_binding(self) -> "GateImplementationEvidence":
        if self.result_digest != self.backing_artifact_digest:
            raise ValueError("gate result digest must equal its backing artifact digest")
        return self


class MVPReadinessEvidencePacket(PilotEvidenceModel):
    schema_version: Literal[MVP_READINESS_SCHEMA_VERSION] = MVP_READINESS_SCHEMA_VERSION
    packet_id: str
    packet_digest: str = ""
    implementation_ready: Literal[True] = True
    capability_claim_authorized: Literal[False] = False
    live_gate0_status: Literal["not_run"] = "not_run"
    live_pilot_status: Literal["not_run"] = "not_run"
    inference_requests_sent: Literal[0] = 0
    release: ImmutableReleaseIdentity
    gate_evidence: tuple[GateEvidenceReference, ...]
    gate0: Gate0OfflineReadiness
    d0: D0FixtureFeasibilityEvidence
    s1: S1OfflineRetentionEvidence
    solve_execution: OfflineSolveExecutionProvenance
    task_audit_manifest_digest: str
    reserved_pilot_task_manifest_digest: str
    pilot_dry_run_manifest_digest: str
    pilot_not_run_report_digest: str
    factory_followup: FactoryFollowupIdentityEvidence
    runtime_sessions: RuntimeSessionIdentitySet
    limitations: tuple[str, ...] = Field(min_length=1)
    file_digests: dict[str, str] = Field(min_length=1)

    @field_validator("packet_id")
    @classmethod
    def validate_packet_id(cls, value: str) -> str:
        return _require_identifier(value, "packet_id")

    @field_validator(
        "task_audit_manifest_digest",
        "reserved_pilot_task_manifest_digest",
        "pilot_dry_run_manifest_digest",
        "pilot_not_run_report_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("MVP limitations may not contain empty entries")
        if len(normalized) != len(set(normalized)):
            raise ValueError("MVP limitations may not duplicate")
        _scan_packet_value(normalized, public=True)
        return normalized

    @field_validator("file_digests")
    @classmethod
    def validate_file_digests(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            _relative_path(path, "packet evidence path"): _require_digest(
                digest,
                "packet evidence digest",
            )
            for path, digest in value.items()
        }
        if MVP_READINESS_MANIFEST_PATH in normalized:
            raise ValueError("readiness manifest cannot include its own file digest")
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def validate_packet(self) -> "MVPReadinessEvidencePacket":
        gate_ids = tuple(reference.gate_id for reference in self.gate_evidence)
        if gate_ids != REQUIRED_MVP_GATES:
            raise ValueError("MVP gate evidence must cover B0 through F1 exactly in canonical order")
        gate_paths = [reference.evidence_path for reference in self.gate_evidence]
        gate_digests = [reference.evidence_digest for reference in self.gate_evidence]
        if len(gate_paths) != len(set(gate_paths)):
            raise ValueError("MVP gate evidence paths may not duplicate")
        if len(gate_digests) != len(set(gate_digests)):
            raise ValueError("MVP gate evidence digests may not be reused across gates")
        if self.s1.final_protocol_digest != self.release.protocol_source_digest:
            raise ValueError("S1 retained protocol crossed the active immutable release")
        if self.s1.epoch_manifest_digest != self.release.epoch_manifest_digest:
            raise ValueError("S1 evidence crossed the active release epoch")
        if self.solve_execution.protocol_source_digest != self.release.protocol_source_digest:
            raise ValueError("offline solve provenance crossed the active release protocol")
        if self.factory_followup.new_release_digest != self.release.release_digest:
            raise ValueError("factory follow-up does not produce the active release")
        if self.factory_followup.new_manifest_digest != self.release.release_manifest_digest:
            raise ValueError("factory follow-up crossed the active release manifest")
        if self.factory_followup.new_protocol_digest != self.release.protocol_source_digest:
            raise ValueError("factory follow-up crossed the active release protocol")
        if (
            self.factory_followup.representative_compiled_semantic_digest
            != self.release.representative_compiled_semantic_digest
        ):
            raise ValueError("factory follow-up crossed the active representative compiled plan")
        if self.factory_followup.dependency_manifest_digest != self.release.dependency_manifest_digest:
            raise ValueError("factory follow-up crossed active runtime dependencies")
        if self.factory_followup.epoch_manifest_digest != self.release.epoch_manifest_digest:
            raise ValueError("factory follow-up crossed the active release epoch")
        if {session.active_release_digest for session in self.runtime_sessions.sessions} != {
            self.release.release_digest
        }:
            raise ValueError("runtime session evidence crossed the active immutable release")
        all_paths = set(self.file_digests)
        missing_public = sorted(REQUIRED_PUBLIC_PATHS - all_paths)
        missing_controlled = sorted(REQUIRED_CONTROLLED_PATHS - all_paths)
        if missing_public or missing_controlled:
            raise ValueError(
                "MVP readiness layout is incomplete: "
                f"public={missing_public}, controlled={missing_controlled}"
            )
        for reference in self.gate_evidence:
            if self.file_digests.get(reference.evidence_path) != reference.evidence_digest:
                raise ValueError(f"gate {reference.gate_id} digest crossed packet file index")
        digests = list(self.file_digests.values())
        if len(digests) != len(set(digests)):
            raise ValueError("MVP evidence packet may not duplicate identical artifacts")
        computed = evidence_digest(
            {
                "kind": MVP_READINESS_SCHEMA_VERSION,
                "packet": self.model_dump(mode="python", exclude={"packet_digest"}),
            }
        )
        if self.packet_digest and self.packet_digest != computed:
            raise ValueError("MVP readiness packet digest mismatch")
        if not self.packet_digest:
            object.__setattr__(self, "packet_digest", computed)
        return self


ArtifactPayload = bytes | bytearray | str | BaseModel | Mapping[str, Any] | Sequence[Any]


def _artifact_bytes(value: ArtifactPayload) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, BaseModel):
        return _canonical_json_bytes(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return _canonical_json_bytes(dict(value))
    if isinstance(value, Sequence):
        return _canonical_json_bytes(list(value))
    raise TypeError(f"unsupported readiness artifact payload {type(value)!r}")


def normalize_mvp_evidence_artifacts(
    artifacts: Mapping[str, ArtifactPayload],
) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    for raw_path, payload in artifacts.items():
        path = _relative_path(raw_path, "MVP evidence artifact path")
        if path == MVP_READINESS_MANIFEST_PATH:
            raise ValueError("caller may not supply the readiness manifest artifact")
        if path in normalized:
            raise ValueError(f"duplicate normalized MVP evidence path: {path}")
        parts = PurePosixPath(path).parts
        if not parts or parts[0] not in {PUBLIC_RELEASE_EVIDENCE_DIR, CONTROLLED_EVIDENCE_DIR}:
            raise ValueError("MVP evidence artifacts must be public-release or controlled evidence")
        normalized[path] = _artifact_bytes(payload)
    return dict(sorted(normalized.items()))


def mvp_evidence_file_digests(
    artifacts: Mapping[str, ArtifactPayload] | Mapping[str, bytes],
) -> dict[str, str]:
    normalized = normalize_mvp_evidence_artifacts(artifacts)
    return {path: _sha256_bytes(raw) for path, raw in normalized.items()}


def _validate_gate_backing_artifact(
    *,
    gate_path: str,
    record: GateImplementationEvidence,
    normalized_artifacts: Mapping[str, bytes],
) -> None:
    if record.backing_artifact_path == gate_path:
        raise PilotEvidenceError(f"gate {record.gate_id} backing artifact cannot be its wrapper")
    raw = normalized_artifacts.get(record.backing_artifact_path)
    if raw is None:
        raise PilotEvidenceError(f"gate {record.gate_id} backing artifact is missing")
    if _sha256_bytes(raw) != record.backing_artifact_digest:
        raise PilotEvidenceError(f"gate {record.gate_id} backing artifact digest mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotEvidenceError(f"gate {record.gate_id} backing artifact is not typed JSON") from exc

    schema = record.backing_artifact_schema
    if schema == "gate0_conformance":
        if record.gate_id != "G0" or record.evidence_kind != "offline_conformance":
            raise PilotEvidenceError("Gate0 backing artifact crossed gate kind")
        Gate0ConformanceReport.model_validate(payload)
    elif schema == "d0_feasibility":
        if record.gate_id != "D0" or record.evidence_kind != "offline_conformance":
            raise PilotEvidenceError("D0 backing artifact crossed gate kind")
        D0FixtureFeasibilityEvidence.model_validate(payload)
    elif schema == "s1_retention":
        if record.gate_id != "S1" or record.evidence_kind != "offline_conformance":
            raise PilotEvidenceError("S1 backing artifact crossed gate kind")
        S1OfflineRetentionEvidence.model_validate(payload)
    elif schema == "factory_followup_message":
        if record.gate_id != "F1b" or record.evidence_kind != "immutable_identity":
            raise PilotEvidenceError("F1b backing artifact crossed gate kind")
        HarnessFactoryMessage.model_validate(payload)
    elif schema == "runtime_session_identity_set":
        if record.gate_id != "F1c" or record.evidence_kind != "immutable_identity":
            raise PilotEvidenceError("F1c backing artifact crossed gate kind")
        RuntimeSessionIdentitySet.model_validate(payload)
    else:
        typed = PilotGateDeterministicTestEvidence.model_validate(payload)
        if typed.gate_id != record.gate_id:
            raise PilotEvidenceError(f"gate {record.gate_id} test evidence crossed gate identity")


def _gate_evidence_references(
    normalized_artifacts: Mapping[str, bytes],
) -> tuple[GateEvidenceReference, ...]:
    references: list[GateEvidenceReference] = []
    for gate_id in REQUIRED_MVP_GATES:
        path = f"{CONTROLLED_EVIDENCE_DIR}/gates/{gate_id}.json"
        raw = normalized_artifacts.get(path)
        if raw is None:
            raise PilotEvidenceError(f"missing implementation evidence for gate {gate_id}")
        try:
            record = GateImplementationEvidence.model_validate(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PilotEvidenceError(f"invalid implementation evidence for gate {gate_id}") from exc
        if record.gate_id != gate_id:
            raise PilotEvidenceError(f"gate evidence path crossed identity for {gate_id}")
        _validate_gate_backing_artifact(
            gate_path=path,
            record=record,
            normalized_artifacts=normalized_artifacts,
        )
        references.append(
            GateEvidenceReference(
                gate_id=gate_id,
                evidence_path=path,
                evidence_digest=_sha256_bytes(raw),
                evidence_kind=record.evidence_kind,
            )
        )
    return tuple(references)


def build_mvp_readiness_evidence_packet(
    *,
    packet_id: str,
    release: ImmutableReleaseIdentity,
    gate0: Gate0OfflineReadiness,
    d0: D0FixtureFeasibilityEvidence,
    s1: S1OfflineRetentionEvidence,
    solve_execution: OfflineSolveExecutionProvenance,
    task_audit: PilotTaskAuditManifest,
    pilot_dry_run: PilotDryRunManifest,
    pilot_report: PilotNotRunDevelopmentReport,
    factory_followup: FactoryFollowupIdentityEvidence,
    runtime_sessions: RuntimeSessionIdentitySet,
    limitations: Sequence[str],
    artifacts: Mapping[str, ArtifactPayload],
) -> MVPReadinessEvidencePacket:
    """Build the content-addressed manifest for a complete no-live evidence layout."""

    normalized = normalize_mvp_evidence_artifacts(artifacts)
    if task_audit.reservation_state != "reserved" or task_audit.reserved_task is None:
        raise PilotEvidenceError("not-run readiness requires one audited, unconsumed pilot reservation")
    reserved = task_audit.reserved_task
    if pilot_dry_run.active_release_digest != release.release_digest:
        raise PilotEvidenceError("pilot dry run crossed the active immutable release")
    if pilot_dry_run.release_manifest_digest != release.release_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed the active release manifest")
    if pilot_dry_run.epoch_manifest_digest != release.epoch_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed the active release epoch")
    if pilot_dry_run.protocol_source_digest != release.protocol_source_digest:
        raise PilotEvidenceError("pilot dry run crossed the released protocol")
    if (
        pilot_dry_run.release_representative_compiled_semantic_digest
        != release.representative_compiled_semantic_digest
    ):
        raise PilotEvidenceError("pilot dry run crossed the released representative compiled plan")
    if pilot_dry_run.dependency_manifest_digest != release.dependency_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed released runtime dependencies")
    if solve_execution.task_manifest_digest != pilot_dry_run.task_manifest_digest:
        raise PilotEvidenceError("offline solve provenance crossed the reserved pilot task")
    if solve_execution.protocol_source_digest != pilot_dry_run.protocol_source_digest:
        raise PilotEvidenceError("offline solve provenance crossed the pilot protocol")
    if (
        solve_execution.pilot_compiled_semantic_digest
        != pilot_dry_run.pilot_compiled_semantic_digest
    ):
        raise PilotEvidenceError("offline solve provenance crossed the pilot compiled plan")
    if solve_execution.environment_digest != pilot_dry_run.environment_digest:
        raise PilotEvidenceError("offline solve provenance crossed the pilot environment")
    if pilot_dry_run.task_audit_manifest_digest != task_audit.audit_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed the task audit manifest")
    if pilot_dry_run.task_manifest_digest != reserved.task_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed the reserved task")
    expected_report = {
        "pilot_id": pilot_dry_run.pilot_id,
        "pilot_manifest_digest": pilot_dry_run.manifest_digest,
        "task_audit_manifest_digest": task_audit.audit_manifest_digest,
        "reserved_task_manifest_digest": reserved.task_manifest_digest,
    }
    crossed_report = [
        field_name
        for field_name, expected_value in expected_report.items()
        if getattr(pilot_report, field_name) != expected_value
    ]
    if crossed_report:
        raise PilotEvidenceError("pilot not-run report crossed: " + ", ".join(crossed_report))
    return MVPReadinessEvidencePacket(
        packet_id=packet_id,
        release=release,
        gate_evidence=_gate_evidence_references(normalized),
        gate0=gate0,
        d0=d0,
        s1=s1,
        solve_execution=solve_execution,
        task_audit_manifest_digest=task_audit.audit_manifest_digest,
        reserved_pilot_task_manifest_digest=reserved.task_manifest_digest,
        pilot_dry_run_manifest_digest=pilot_dry_run.manifest_digest,
        pilot_not_run_report_digest=pilot_report.report_digest,
        factory_followup=factory_followup,
        runtime_sessions=runtime_sessions,
        limitations=tuple(limitations),
        file_digests={path: _sha256_bytes(raw) for path, raw in normalized.items()},
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotEvidenceError(f"evidence artifact is not valid UTF-8 JSON: {path.name}") from exc


def _read_jsonl(path: Path) -> tuple[Any, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PilotEvidenceError(f"evidence artifact is not valid UTF-8 JSONL: {path.name}") from exc
    rows: list[Any] = []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PilotEvidenceError(
                f"evidence artifact contains invalid JSONL at {path.name}:{line_no}"
            ) from exc
    if not rows:
        raise PilotEvidenceError(f"required JSONL evidence may not be empty: {path.name}")
    return tuple(rows)


def _actual_packet_file_digests(root: Path) -> dict[str, str]:
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PilotEvidenceError("MVP evidence packet may not contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MVP_READINESS_MANIFEST_PATH:
            continue
        relative = _relative_path(relative, "packet artifact path")
        parts = PurePosixPath(relative).parts
        if parts[0] not in {PUBLIC_RELEASE_EVIDENCE_DIR, CONTROLLED_EVIDENCE_DIR}:
            raise PilotEvidenceError("unexpected artifact outside required evidence roots")
        actual[relative] = _sha256_bytes(path.read_bytes())
    return dict(sorted(actual.items()))


def _validate_run_layout(file_paths: set[str]) -> None:
    prefix = f"{CONTROLLED_EVIDENCE_DIR}/runs/"
    run_paths = sorted(path for path in file_paths if path.startswith(prefix))
    if not run_paths:
        raise PilotEvidenceError("controlled evidence requires at least one exact run proof layout")
    run_ids: set[str] = set()
    for path in run_paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[1] != "runs":
            raise PilotEvidenceError("invalid controlled run evidence path")
        run_ids.add(parts[2])
    for run_id in run_ids:
        root = f"{CONTROLLED_EVIDENCE_DIR}/runs/{run_id}"
        required = {
            f"{root}/run_manifest.json",
            f"{root}/{HARNESS_SOLVE_RESULT_FILE}",
            f"{root}/{CONTROLLED_RUN_EVIDENCE_REF}",
            f"{root}/tool_and_side_effect_receipts.jsonl",
        }
        missing = sorted(required - file_paths)
        if missing:
            raise PilotEvidenceError(f"run {run_id!r} evidence is incomplete: {missing}")
        if not any(path.startswith(f"{root}/pre_call_contexts/") for path in file_paths):
            raise PilotEvidenceError(f"run {run_id!r} lacks exact pre-call context evidence")
        if not any(path.startswith(f"{root}/artifacts/") for path in file_paths):
            raise PilotEvidenceError(f"run {run_id!r} lacks exact artifact evidence")


def _controlled_run_roots(file_paths: set[str]) -> tuple[str, ...]:
    prefix = f"{CONTROLLED_EVIDENCE_DIR}/runs/"
    roots = {
        "/".join(PurePosixPath(path).parts[:3])
        for path in file_paths
        if path.startswith(prefix) and len(PurePosixPath(path).parts) >= 4
    }
    return tuple(sorted(roots))


def _load_pilot_evaluation_contract(controlled_root: Path) -> PilotEvaluationContractEvidence:
    return PilotEvaluationContractEvidence.model_validate(
        _read_json(controlled_root / "evaluation_contract.json")
    )


def _load_outcome_receipts(controlled_root: Path) -> tuple[OutcomeReceipt, ...]:
    rows = _read_jsonl(controlled_root / "evaluator/outcome_receipts.jsonl")
    try:
        receipts = tuple(OutcomeReceipt.model_validate(row) for row in rows)
    except ValueError as exc:
        raise PilotEvidenceError("pilot outcome receipts are not typed OutcomeReceipt rows") from exc
    if not receipts:
        raise PilotEvidenceError("pilot outcome receipts may not be empty")
    digests = [receipt.receipt_digest for receipt in receipts]
    if len(digests) != len(set(digests)):
        raise PilotEvidenceError("pilot outcome receipts may not duplicate")
    return receipts


def _load_raw_paired_outcomes(controlled_root: Path) -> tuple[PilotRawPairedOutcomeRecord, ...]:
    rows = _read_jsonl(controlled_root / "analysis/raw_paired_outcomes.jsonl")
    try:
        outcomes = tuple(PilotRawPairedOutcomeRecord.model_validate(row) for row in rows)
    except ValueError as exc:
        raise PilotEvidenceError("raw paired outcomes are not typed P1 paired outcome rows") from exc
    if not outcomes:
        raise PilotEvidenceError("raw paired outcomes may not be empty")
    keys = [row.pair_key_digest for row in outcomes]
    if len(keys) != len(set(keys)):
        raise PilotEvidenceError("raw paired outcomes may not duplicate PairKeys")
    return outcomes


def _validate_evaluator_outcome_evidence(
    *,
    controlled_root: Path,
    task: TaskEnvelope,
    pilot: PilotDryRunManifest,
) -> None:
    contract = _load_pilot_evaluation_contract(controlled_root)
    for call in pilot.evaluator_calls:
        if (
            call.evaluator_id != contract.evaluator_id
            or call.evaluator_identity_digest != contract.evaluator_identity_digest
            or call.evaluation_contract_digest != contract.evaluation_contract_digest
        ):
            raise PilotEvidenceError("pilot evaluator call crossed the evaluation contract")

    receipts = _load_outcome_receipts(controlled_root)
    receipt_by_digest = {receipt.receipt_digest: receipt for receipt in receipts}
    for receipt in receipts:
        if receipt.execution_mode != "deterministic_replay":
            raise PilotEvidenceError("P1 offline outcome receipts must be deterministic replay")
        if receipt.live_inference_status != "not_run" or receipt.real_inference_requests_sent != 0:
            raise PilotEvidenceError("P1 offline outcome receipts must remain no-live")
        if receipt.task_manifest_digest != task.task_manifest_digest:
            raise PilotEvidenceError("pilot outcome receipt crossed the reserved task")
        if receipt.evaluation_contract_digest != contract.evaluation_contract_digest:
            raise PilotEvidenceError("pilot outcome receipt crossed the evaluation contract")
        if (
            receipt.evaluator_id != contract.evaluator_id
            or receipt.evaluator_identity_digest != contract.evaluator_identity_digest
        ):
            raise PilotEvidenceError("pilot outcome receipt crossed evaluator authority")

    raw_pairs = _load_raw_paired_outcomes(controlled_root)
    saw_child_for_pilot = False
    for row in raw_pairs:
        parent = receipt_by_digest.get(row.parent_receipt_digest)
        child = receipt_by_digest.get(row.child_receipt_digest)
        if parent is None or child is None:
            raise PilotEvidenceError("raw paired outcome references a missing outcome receipt")
        parent_pair_digest = pair_key_digest(parent.pair_key)
        child_pair_digest = pair_key_digest(child.pair_key)
        if parent_pair_digest != row.pair_key_digest or child_pair_digest != row.pair_key_digest:
            raise PilotEvidenceError("raw paired outcome crossed PairKey identity")
        if parent.complete_repair != row.parent_complete_repair:
            raise PilotEvidenceError("raw paired outcome crossed parent outcome value")
        if child.complete_repair != row.child_complete_repair:
            raise PilotEvidenceError("raw paired outcome crossed child outcome value")
        if child.protocol_digest == pilot.protocol_source_digest:
            saw_child_for_pilot = True
    if not saw_child_for_pilot:
        raise PilotEvidenceError("raw paired outcomes do not include the pilot protocol")


def _raw_paired_outcomes_digest(outcomes: Sequence[PilotRawPairedOutcomeRecord]) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-pilot-raw-paired-outcomes-v1",
            "outcomes": [outcome.model_dump(mode="python") for outcome in outcomes],
        }
    )


def _validate_content_null_intervention(controlled_root: Path) -> None:
    intervention = PilotContentNullInterventionEvidence.model_validate(
        _read_json(controlled_root / "interventions/content_null_manifest.json")
    )
    paired = _load_raw_paired_outcomes(controlled_root)
    if intervention.paired_outcome_digest != _raw_paired_outcomes_digest(paired):
        raise PilotEvidenceError("content-null intervention crossed raw paired outcomes")


def _validate_d0_source_projection(controlled_root: Path, packet: MVPReadinessEvidencePacket) -> None:
    source = DevelopmentTaskFeasibilityManifest.model_validate(
        _read_json(controlled_root / "d0/fixture_feasibility_manifest.json")
    )
    rebuilt = D0FixtureFeasibilityEvidence.from_manifest(source)
    if rebuilt != packet.d0:
        raise PilotEvidenceError("D0 fixture feasibility source crossed packet projection")


def _validate_s1_public_lineage(public_root: Path, packet: MVPReadinessEvidencePacket) -> None:
    rows = _read_jsonl(public_root / "search/transaction_lineage_public.jsonl")
    try:
        lineage = tuple(PublicSearchLineageRecord.model_validate(row) for row in rows)
    except ValueError as exc:
        raise PilotEvidenceError("public S1 lineage rows are not typed lineage records") from exc
    accepted = {
        record.transaction_digest: record
        for record in lineage
        if record.status == "accepted" and record.operator != "instruction_rewrite"
    }
    for retained in packet.s1.retained_structural_transactions:
        record = accepted.get(retained.transaction_digest)
        if record is None:
            raise PilotEvidenceError("S1 retained transaction missing from public lineage")
        if (
            record.transaction_id != retained.transaction_id
            or record.operator != retained.operator
            or record.parent_protocol_digest != retained.parent_protocol_digest
            or record.child_protocol_digest != retained.child_protocol_digest
        ):
            raise PilotEvidenceError("S1 retained transaction crossed public lineage")


def _validate_factory_source_projection(controlled_root: Path, packet: MVPReadinessEvidencePacket) -> None:
    chat = HarnessFactoryChatManifest.model_validate(
        _read_json(controlled_root / "factory/chat_manifest.json")
    )
    message = HarnessFactoryMessage.model_validate(
        _read_json(controlled_root / "factory/followup_message.json")
    )
    rebuilt = FactoryFollowupIdentityEvidence.from_records(chat=chat, message=message)
    if rebuilt != packet.factory_followup:
        raise PilotEvidenceError("factory follow-up source records crossed packet projection")


def _validate_session_source_projection(controlled_root: Path, packet: MVPReadinessEvidencePacket) -> None:
    continued = HarnessSessionManifest.model_validate(
        _read_json(controlled_root / "sessions/same_release_continuation.json")
    )
    new = HarnessSessionManifest.model_validate(
        _read_json(controlled_root / "sessions/independent_new_session.json")
    )
    rebuilt = RuntimeSessionIdentitySet(
        sessions=(
            RuntimeSessionIdentityEvidence.from_manifest(
                continued,
                role="same_release_continuation",
            ),
            RuntimeSessionIdentityEvidence.from_manifest(
                new,
                role="independent_new_session",
            ),
        )
    )
    if rebuilt != packet.runtime_sessions:
        raise PilotEvidenceError("runtime session source records crossed packet projection")


def _validate_controlled_run_evidence(
    *,
    root: Path,
    file_paths: set[str],
    packet: MVPReadinessEvidencePacket,
    pilot: PilotDryRunManifest,
) -> None:
    matched_solve_result = False
    expected_model_call_ids = {call.call_id for call in pilot.model_calls}
    expected_public_steps = {call.step_id for call in pilot.public_verification_calls}
    for run_root in _controlled_run_roots(file_paths):
        run_path = root / PurePosixPath(run_root)
        solve = HarnessSolveResult.model_validate(
            _read_json(run_path / HARNESS_SOLVE_RESULT_FILE)
        )
        evidence = RunEvidence.model_validate(
            _read_json(run_path / PurePosixPath(CONTROLLED_RUN_EVIDENCE_REF))
        )
        expected_pair_digest = pair_key_digest(evidence.pair_key)
        if PurePosixPath(run_root).parts[-1] != expected_pair_digest:
            raise PilotEvidenceError("controlled run directory crossed PairKey digest")
        if solve.controlled_run_evidence is None:
            raise PilotEvidenceError("harness solve result lacks controlled RunEvidence reference")
        reference = solve.controlled_run_evidence
        if reference.evidence_id != evidence.evidence_id:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed evidence_id")
        if reference.evidence_digest != evidence.evidence_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed evidence_digest")
        if reference.pair_key_digest != expected_pair_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed PairKey")
        if reference.runtime_environment_digest != evidence.environment.runtime_environment_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed environment")
        if reference.release_digest != pilot.active_release_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed release")
        if reference.release_manifest_digest != pilot.release_manifest_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed release manifest")
        if reference.task_manifest_digest != pilot.task_manifest_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed pilot task")
        if reference.protocol_digest != pilot.protocol_source_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed pilot protocol")
        if reference.compiled_semantic_digest != pilot.pilot_compiled_semantic_digest:
            raise PilotEvidenceError("solve controlled RunEvidence reference crossed compiled plan")
        if evidence.execution_mode != "deterministic_replay":
            raise PilotEvidenceError("P1 offline RunEvidence must be deterministic replay")
        if evidence.live_inference_status != "not_run" or evidence.real_inference_requests_sent != 0:
            raise PilotEvidenceError("P1 offline RunEvidence must remain no-live")
        if evidence.task_manifest_digest != pilot.task_manifest_digest:
            raise PilotEvidenceError("RunEvidence crossed the pilot task")
        if evidence.protocol_digest != pilot.protocol_source_digest:
            raise PilotEvidenceError("RunEvidence crossed the pilot protocol")
        if evidence.compiled_semantic_digest != pilot.pilot_compiled_semantic_digest:
            raise PilotEvidenceError("RunEvidence crossed the pilot compiled plan")
        if evidence.environment.runtime_environment_digest != pilot.environment_digest:
            raise PilotEvidenceError("RunEvidence crossed the pilot environment")
        if {call.call_id for call in evidence.provider_calls} != expected_model_call_ids:
            raise PilotEvidenceError("RunEvidence provider calls crossed the pilot model-call manifest")
        if {
            receipt.verification_step_id
            for receipt in evidence.tool_receipts
            if receipt.phase == "terminal_public_verification"
        } != expected_public_steps:
            raise PilotEvidenceError("RunEvidence public verification crossed the pilot manifest")
        if solve.execution_mode != "deterministic_replay":
            raise PilotEvidenceError("P1 offline solve result must be deterministic replay")
        if solve.live_inference_status != "not_run" or solve.real_inference_requests_sent != 0:
            raise PilotEvidenceError("P1 offline solve result must remain no-live")
        if solve.task.task_envelope_digest != pilot.task_manifest_digest:
            raise PilotEvidenceError("harness solve result crossed the pilot task")
        if solve.release.release_digest != pilot.active_release_digest:
            raise PilotEvidenceError("harness solve result crossed active release")
        if solve.release.release_manifest_digest != pilot.release_manifest_digest:
            raise PilotEvidenceError("harness solve result crossed active release manifest")
        if solve.release.protocol_source_digest != pilot.protocol_source_digest:
            raise PilotEvidenceError("harness solve result crossed pilot protocol")
        if solve.compiled.compiled_semantic_digest != pilot.pilot_compiled_semantic_digest:
            raise PilotEvidenceError("harness solve result crossed pilot compiled plan")
        if solve.result_digest == packet.solve_execution.solve_result_digest:
            if packet.solve_execution.replay_fixture_digest != evidence.evidence_digest:
                raise PilotEvidenceError("offline solve provenance crossed controlled RunEvidence")
            matched_solve_result = True
    if not matched_solve_result:
        raise PilotEvidenceError("offline solve provenance lacks a matching harness solve result")


def _scan_packet_tree(
    root: Path,
    *,
    canary_values: Sequence[str | bytes],
    canary_digests: Sequence[str],
) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MVP_READINESS_MANIFEST_PATH:
            value = _read_json(path)
            _scan_packet_value(
                value,
                public=False,
                canary_values=canary_values,
                canary_digests=canary_digests,
            )
            continue
        public = relative.startswith(f"{PUBLIC_RELEASE_EVIDENCE_DIR}/")
        suffix = path.suffix.casefold()
        if suffix == ".json":
            values = (_read_json(path),)
        elif suffix == ".jsonl":
            values = _read_jsonl(path)
        else:
            try:
                values = (path.read_text(encoding="utf-8"),)
            except UnicodeDecodeError as exc:
                raise PilotEvidenceError("MVP readiness evidence must be inspectable UTF-8 text") from exc
        allowed_absolute_paths: tuple[str, ...] = ()
        if relative == (
            f"{CONTROLLED_EVIDENCE_DIR}/evaluator/task_audit_manifest.json"
        ):
            audit = PilotTaskAuditManifest.model_validate(values[0])
            allowed_absolute_paths = tuple(
                TaskEnvelope.model_validate(task.public_projection).workspace_snapshot.uri
                for task in audit.tasks
            )
        for value in values:
            _scan_packet_value(
                value,
                public=public,
                canary_values=canary_values,
                canary_digests=canary_digests,
                allowed_absolute_paths=allowed_absolute_paths,
            )


def _parse_limitations(path: Path) -> tuple[str, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    limitations = tuple(line[2:].strip() for line in lines if line.startswith("- "))
    if not limitations or any(not item for item in limitations):
        raise PilotEvidenceError("public limitations.md must contain explicit bullet limitations")
    return limitations


def _assert_pilot_against_packet_tree(
    *,
    packet: MVPReadinessEvidencePacket,
    task: TaskEnvelope,
    audit: PilotTaskAuditManifest,
    pilot_plan: CompositeRunPlan,
    pilot: PilotDryRunManifest,
    pilot_report: PilotNotRunDevelopmentReport,
) -> None:
    if audit.reservation_state != "reserved" or audit.reserved_task is None:
        raise PilotEvidenceError("not-run packet requires one audited, unconsumed pilot reservation")
    reserved = audit.reserved_task
    if reserved.public_projection != task_envelope_public_projection(task):
        raise PilotEvidenceError("controlled task public manifest crossed the audited projection")
    expected = {
        "task_audit_manifest_digest": audit.audit_manifest_digest,
        "reserved_pilot_task_manifest_digest": reserved.task_manifest_digest,
        "pilot_dry_run_manifest_digest": pilot.manifest_digest,
        "pilot_not_run_report_digest": pilot_report.report_digest,
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(packet, field_name) != expected_value
    ]
    if crossed:
        raise PilotEvidenceError("readiness packet crossed pilot evidence: " + ", ".join(crossed))
    if pilot.task_manifest_digest != task.task_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed controlled public task identity")
    if pilot.task_public_projection_digest != reserved.public_projection_digest:
        raise PilotEvidenceError("pilot dry run crossed audited public projection identity")
    if pilot.task_audit_manifest_digest != audit.audit_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed audit identity")
    if pilot.active_release_digest != packet.release.release_digest:
        raise PilotEvidenceError("pilot dry run crossed active release")
    if pilot.release_manifest_digest != packet.release.release_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed release manifest")
    if pilot.epoch_manifest_digest != packet.release.epoch_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed release epoch")
    if pilot.deployment != packet.release.deployment:
        raise PilotEvidenceError("pilot dry run crossed release deployment")
    if pilot.protocol_source_digest != packet.release.protocol_source_digest:
        raise PilotEvidenceError("pilot dry run crossed released protocol")
    if (
        pilot.release_representative_compiled_semantic_digest
        != packet.release.representative_compiled_semantic_digest
    ):
        raise PilotEvidenceError("pilot dry run crossed released representative compiled plan")
    if pilot.dependency_manifest_digest != packet.release.dependency_manifest_digest:
        raise PilotEvidenceError("pilot dry run crossed released dependencies")
    if pilot_plan.task_envelope_digest != task.task_manifest_digest:
        raise PilotEvidenceError("controlled pilot compiled plan crossed the held-out task")
    if pilot_plan.source_protocol_digest != packet.release.protocol_source_digest:
        raise PilotEvidenceError("controlled pilot compiled plan crossed the released protocol")
    if pilot_plan.dependency_manifest_digest != packet.release.dependency_manifest_digest:
        raise PilotEvidenceError("controlled pilot compiled plan crossed released dependencies")
    if pilot_plan.compiled_semantic_digest != pilot.pilot_compiled_semantic_digest:
        raise PilotEvidenceError("pilot dry run crossed the controlled pilot compiled plan")
    if pilot.model_calls != _planned_model_calls(pilot_plan):
        raise PilotEvidenceError("pilot model-call manifest crossed the held-out compiled plan")
    if pilot.public_verification_calls != _planned_public_calls(pilot_plan):
        raise PilotEvidenceError("pilot public calls crossed the held-out verification plan")
    allowed_tools = {call.call_id: set(call.tool_ids) for call in pilot_plan.actor_calls}
    dependency_tools = {tool.tool_id for tool in pilot_plan.dependency_manifest.trusted_tools}
    for call in pilot.tool_calls:
        if call.actor_call_id not in allowed_tools or call.tool_id not in allowed_tools[call.actor_call_id]:
            raise PilotEvidenceError("pilot tool-call manifest crossed actor authority")
        if call.tool_id not in dependency_tools:
            raise PilotEvidenceError("pilot tool-call manifest crossed dependency authority")
    if pilot.tool_manifest_digest != pilot_tool_manifest_digest(pilot_plan):
        raise PilotEvidenceError("pilot tool manifest identity mismatch")
    if pilot.budget.ceilings != pilot_plan.budget_ledger.aggregate_ceiling:
        raise PilotEvidenceError("pilot budget crossed held-out compiled plan")
    matching_sessions = [
        session
        for session in packet.runtime_sessions.sessions
        if session.session_id == pilot.session_id
        and session.session_manifest_digest == pilot.session_manifest_digest
    ]
    if len(matching_sessions) != 1:
        raise PilotEvidenceError("pilot dry run crossed runtime session identity")
    if pilot_report.pilot_id != pilot.pilot_id:
        raise PilotEvidenceError("pilot not-run report crossed pilot identity")
    if pilot_report.pilot_manifest_digest != pilot.manifest_digest:
        raise PilotEvidenceError("pilot not-run report crossed dry-run manifest")
    if pilot_report.task_audit_manifest_digest != audit.audit_manifest_digest:
        raise PilotEvidenceError("pilot not-run report crossed audit identity")
    if pilot_report.reserved_task_manifest_digest != reserved.task_manifest_digest:
        raise PilotEvidenceError("pilot not-run report crossed reserved task")
    for evidence_path in pilot.evidence_paths:
        path = evidence_path.relative_path.rstrip("/")
        if path in packet.file_digests:
            continue
        if not any(candidate.startswith(path + "/") for candidate in packet.file_digests):
            raise PilotEvidenceError(
                f"pilot evidence path has no packet artifact: {evidence_path.relative_path}"
            )


def validate_mvp_readiness_evidence_packet(
    generation_path: str | Path,
    *,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> MVPReadinessEvidencePacket:
    """Replay all packet identities and authority boundaries from immutable files."""

    root = Path(generation_path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"MVP readiness packet is missing: {root}")
    manifest_path = root / MVP_READINESS_MANIFEST_PATH
    if not manifest_path.is_file():
        raise FileNotFoundError("MVP readiness packet manifest is missing")
    packet = MVPReadinessEvidencePacket.model_validate(_read_json(manifest_path))
    if root.name != packet.packet_digest:
        raise PilotEvidenceError("MVP readiness generation directory is not content-addressed")
    actual = _actual_packet_file_digests(root)
    if actual != packet.file_digests:
        missing = sorted(set(packet.file_digests) - set(actual))
        unexpected = sorted(set(actual) - set(packet.file_digests))
        changed = sorted(
            path
            for path in set(actual) & set(packet.file_digests)
            if actual[path] != packet.file_digests[path]
        )
        raise PilotEvidenceError(
            f"MVP readiness replay mismatch: missing={missing}, unexpected={unexpected}, changed={changed}"
        )
    file_paths = set(actual)
    missing_public = sorted(REQUIRED_PUBLIC_PATHS - file_paths)
    missing_controlled = sorted(REQUIRED_CONTROLLED_PATHS - file_paths)
    if missing_public or missing_controlled:
        raise PilotEvidenceError(
            f"MVP readiness layout missing public={missing_public}, controlled={missing_controlled}"
        )
    _validate_run_layout(file_paths)
    _scan_packet_tree(
        root,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )

    public_root = root / PUBLIC_RELEASE_EVIDENCE_DIR
    controlled_root = root / CONTROLLED_EVIDENCE_DIR
    release_manifest = HarnessReleaseManifest.model_validate(
        _read_json(public_root / "release_manifest.json")
    )
    release_identity = ImmutableReleaseIdentity(
        release_digest=release_manifest.release_digest,
        release_manifest_digest=release_manifest.manifest_digest,
        release_path=f"releases/{release_manifest.release_digest}",
        epoch_id=release_manifest.epoch_id,
        epoch_manifest_digest=release_manifest.epoch_manifest_digest,
        deployment=release_manifest.deployment,
        protocol_source_digest=release_manifest.protocol_source_digest,
        representative_compiled_semantic_digest=release_manifest.compiled_semantic_digest,
        dependency_manifest_digest=release_manifest.dependency_manifest_digest,
        profile_digest=release_manifest.profile_digest,
    )
    if release_identity != packet.release:
        raise PilotEvidenceError("packet release identity crossed immutable release manifest")
    protocol = HarnessProtocol.model_validate(_read_json(public_root / "protocol/source.json"))
    representative_plan = CompositeRunPlan.model_validate(
        _read_json(public_root / "protocol/compiled_plan.json")
    )
    dependencies = RuntimeDependencyManifest.model_validate(
        _read_json(public_root / "runtime/dependency_manifest.json")
    )
    if protocol.source_digest() != release_manifest.protocol_source_digest:
        raise PilotEvidenceError("public protocol crossed immutable release identity")
    if representative_plan.source_protocol_digest != protocol.source_digest():
        raise PilotEvidenceError("public compiled plan crossed protocol identity")
    if (
        representative_plan.compiled_semantic_digest
        != release_manifest.compiled_semantic_digest
    ):
        raise PilotEvidenceError("public compiled plan crossed immutable release identity")
    if representative_plan.dependency_manifest != dependencies:
        raise PilotEvidenceError("public dependency manifest crossed compiled plan")
    if dependencies.manifest_digest() != release_manifest.dependency_manifest_digest:
        raise PilotEvidenceError("public dependencies crossed immutable release identity")

    evidence_index = PublicEvidenceIndex.model_validate(
        _read_json(public_root / "evidence_index.json")
    )
    if (
        evidence_index.protocol_source_digest != release_manifest.protocol_source_digest
        or evidence_index.compiled_semantic_digest != release_manifest.compiled_semantic_digest
        or evidence_index.dependency_manifest_digest != release_manifest.dependency_manifest_digest
        or evidence_index.epoch_manifest_digest != release_manifest.epoch_manifest_digest
        or evidence_index.profile_digest != release_manifest.profile_digest
    ):
        raise PilotEvidenceError("public evidence index crossed immutable release identity")
    for relative, digest in evidence_index.artifacts.items():
        packet_path = f"{PUBLIC_RELEASE_EVIDENCE_DIR}/{relative}"
        if packet.file_digests.get(packet_path) != digest:
            raise PilotEvidenceError(f"public evidence index mismatch for {relative}")
    for packet_path, digest in packet.file_digests.items():
        if not packet_path.startswith(f"{PUBLIC_RELEASE_EVIDENCE_DIR}/"):
            continue
        if packet_path.endswith("/release_manifest.json"):
            continue
        release_digest = release_manifest.file_digests.get(packet_path)
        if release_digest != digest:
            raise PilotEvidenceError(f"public packet artifact is outside immutable release: {packet_path}")

    preregistration = Gate0PreregistrationPublic.model_validate(
        _read_json(public_root / "gate0_preregistration.json")
    )
    gate0_live_report = Gate0NotRunReport.model_validate(
        _read_json(public_root / "gate0_report.json")
    )
    pilot_summary = PilotNotRunSummary.model_validate(
        _read_json(public_root / "pilot_summary.json")
    )
    limitations = _parse_limitations(public_root / "limitations.md")
    if limitations != packet.limitations:
        raise PilotEvidenceError("public limitations crossed readiness packet")

    task = TaskEnvelope.model_validate(_read_json(controlled_root / "task_public_manifest.json"))
    if task.data_state != "development":
        raise PilotEvidenceError("sealed-confirmation task entered controlled pilot evidence")
    canonical_task = task_envelope_public_projection(task)
    if canonical_task != _read_json(controlled_root / "task_public_manifest.json"):
        raise PilotEvidenceError("controlled task manifest is not the canonical public projection")
    audit = PilotTaskAuditManifest.model_validate(
        _read_json(controlled_root / "evaluator/task_audit_manifest.json")
    )
    gate0_manifest = Gate0DryRunManifest.model_validate(
        _read_json(controlled_root / "gate0/dry_run_manifest.json")
    )
    gate0_conformance = Gate0ConformanceReport.model_validate(
        _read_json(controlled_root / "gate0/deterministic_conformance_report.json")
    )
    replayed_gate0 = Gate0OfflineReadiness.from_records(
        manifest=gate0_manifest,
        conformance=gate0_conformance,
        preregistration=preregistration,
        live_report=gate0_live_report,
    )
    if replayed_gate0 != packet.gate0:
        raise PilotEvidenceError("Gate0 readiness projection crossed packet")
    d0 = D0FixtureFeasibilityEvidence.model_validate(
        _read_json(controlled_root / "d0/fixture_feasibility.json")
    )
    if d0 != packet.d0:
        raise PilotEvidenceError("D0 fixture feasibility crossed packet")
    _validate_d0_source_projection(controlled_root, packet)
    s1 = S1OfflineRetentionEvidence.model_validate(
        _read_json(controlled_root / "search/s1_offline_retention.json")
    )
    if s1 != packet.s1:
        raise PilotEvidenceError("S1 offline retention evidence crossed packet")
    _validate_s1_public_lineage(public_root, packet)
    solve_execution = OfflineSolveExecutionProvenance.model_validate(
        _read_json(controlled_root / "analysis/offline_solve_execution_provenance.json")
    )
    if solve_execution != packet.solve_execution:
        raise PilotEvidenceError("offline solve execution provenance crossed packet")
    pilot = PilotDryRunManifest.model_validate(
        _read_json(controlled_root / "pilot/dry_run_manifest.json")
    )
    pilot_plan = CompositeRunPlan.model_validate(
        _read_json(controlled_root / "pilot/compiled_plan.json")
    )
    factory = FactoryFollowupIdentityEvidence.model_validate(
        _read_json(controlled_root / "factory/followup_transaction_identity.json")
    )
    if factory != packet.factory_followup:
        raise PilotEvidenceError("factory follow-up identity evidence crossed packet")
    _validate_factory_source_projection(controlled_root, packet)
    sessions = RuntimeSessionIdentitySet.model_validate(
        _read_json(controlled_root / "sessions/session_identities.json")
    )
    if sessions != packet.runtime_sessions:
        raise PilotEvidenceError("runtime session identity evidence crossed packet")
    _validate_session_source_projection(controlled_root, packet)
    pilot_report = PilotNotRunDevelopmentReport.model_validate(
        _read_json(controlled_root / "analysis/pilot_report.json")
    )
    _assert_pilot_against_packet_tree(
        packet=packet,
        task=task,
        audit=audit,
        pilot_plan=pilot_plan,
        pilot=pilot,
        pilot_report=pilot_report,
    )
    if pilot_summary.status != "not_run" or pilot_summary.pilot_id != pilot.pilot_id:
        raise PilotEvidenceError("public pilot summary crossed not-run pilot identity")
    if pilot_summary.planned_task_manifest_digest != task.task_manifest_digest:
        raise PilotEvidenceError("public pilot summary crossed reserved task")
    _validate_evaluator_outcome_evidence(
        controlled_root=controlled_root,
        task=task,
        pilot=pilot,
    )
    _validate_content_null_intervention(controlled_root)
    _validate_controlled_run_evidence(
        root=root,
        file_paths=file_paths,
        packet=packet,
        pilot=pilot,
    )

    for reference in packet.gate_evidence:
        path = root / PurePosixPath(reference.evidence_path)
        gate = GateImplementationEvidence.model_validate(_read_json(path))
        if gate.gate_id != reference.gate_id or gate.evidence_kind != reference.evidence_kind:
            raise PilotEvidenceError(f"gate {reference.gate_id} evidence crossed its reference")
        if _sha256_bytes(path.read_bytes()) != reference.evidence_digest:
            raise PilotEvidenceError(f"gate {reference.gate_id} raw evidence digest mismatch")
    return packet


def _write_bytes_once(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def write_mvp_readiness_evidence_packet(
    output_root: str | Path,
    *,
    packet: MVPReadinessEvidencePacket,
    artifacts: Mapping[str, ArtifactPayload],
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> Path:
    """Atomically publish one immutable, content-addressed readiness packet generation."""

    normalized = normalize_mvp_evidence_artifacts(artifacts)
    actual_digests = {path: _sha256_bytes(raw) for path, raw in normalized.items()}
    if actual_digests != packet.file_digests:
        raise PilotEvidenceError("packet artifact bytes do not match the frozen file index")
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    final = root / packet.packet_digest
    if final.exists():
        existing = validate_mvp_readiness_evidence_packet(
            final,
            canary_values=canary_values,
            canary_digests=canary_digests,
        )
        if existing != packet:
            raise PilotEvidenceError("existing immutable readiness generation differs")
        return final
    stage_root = root / f".mvp-readiness-{uuid.uuid4().hex}.tmp"
    stage = stage_root / packet.packet_digest
    stage.mkdir(parents=True)
    try:
        for relative, raw in normalized.items():
            _write_bytes_once(stage / PurePosixPath(relative), raw)
        _write_bytes_once(
            stage / MVP_READINESS_MANIFEST_PATH,
            _canonical_json_bytes(packet.model_dump(mode="json", exclude_none=True)),
        )
        validate_mvp_readiness_evidence_packet(
            stage,
            canary_values=canary_values,
            canary_digests=canary_digests,
        )
        try:
            stage.replace(final)
        except FileExistsError:
            existing = validate_mvp_readiness_evidence_packet(
                final,
                canary_values=canary_values,
                canary_digests=canary_digests,
            )
            if existing != packet:
                raise PilotEvidenceError("concurrent immutable readiness generation differs")
            return final
        return final
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)


def replay_mvp_readiness_evidence_packet(
    generation_path: str | Path,
    *,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> MVPReadinessEvidencePacket:
    return validate_mvp_readiness_evidence_packet(
        generation_path,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )


__all__ = [
    "ArtifactPayload",
    "AuditedDevelopmentTask",
    "CONTROLLED_EVIDENCE_DIR",
    "D0FixtureFeasibilityEvidence",
    "FactoryFollowupIdentityEvidence",
    "Gate0OfflineReadiness",
    "GateEvidenceReference",
    "GateImplementationEvidence",
    "ImmutableReleaseIdentity",
    "MVPReadinessEvidencePacket",
    "MVP_READINESS_MANIFEST_PATH",
    "MVP_READINESS_SCHEMA_VERSION",
    "PILOT_DRY_RUN_SCHEMA_VERSION",
    "PUBLIC_RELEASE_EVIDENCE_DIR",
    "PilotBudgetPlan",
    "PilotContentNullInterventionEvidence",
    "PilotDryRunManifest",
    "PilotEvaluatorCall",
    "PilotEvidenceError",
    "PilotEvidencePath",
    "PilotEvaluationContractEvidence",
    "PilotGateDeterministicTestEvidence",
    "PilotLiveExecutionAuthorization",
    "PilotModelCall",
    "PilotNotRunDevelopmentReport",
    "OfflineSolveExecutionProvenance",
    "PilotPublicVerificationCall",
    "PilotRawPairedOutcomeRecord",
    "PilotReservationEvent",
    "PilotTaskAuditManifest",
    "PilotToolCall",
    "REQUIRED_CONTROLLED_PATHS",
    "REQUIRED_MVP_GATES",
    "REQUIRED_PUBLIC_PATHS",
    "RetainedStructuralTransactionEvidence",
    "RuntimeSessionIdentityEvidence",
    "RuntimeSessionIdentitySet",
    "S1OfflineRetentionEvidence",
    "TASK_AUDIT_SCHEMA_VERSION",
    "audit_public_development_tasks",
    "build_mvp_readiness_evidence_packet",
    "build_pilot_dry_run_manifest",
    "consume_reserved_pilot_task",
    "gate0_conformance_report_digest",
    "mvp_evidence_file_digests",
    "normalize_mvp_evidence_artifacts",
    "pilot_tool_manifest_digest",
    "replay_mvp_readiness_evidence_packet",
    "require_pilot_live_authorization",
    "reserve_audited_pilot_task",
    "validate_mvp_readiness_evidence_packet",
    "validate_pilot_dry_run_bindings",
    "write_mvp_readiness_evidence_packet",
]
