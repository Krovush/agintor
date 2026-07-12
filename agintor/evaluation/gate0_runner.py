from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from ..core.identity import evidence_digest
from ..runtime.harness_profile import (
    HarnessDeploymentProfile,
    harness_deployment_profile_digest,
)
from ..runtime.kernel.composite_provider import CredentialReference
from .gate0 import (
    Gate0AnalysisReport,
    Gate0Arm,
    Gate0ArmPlan,
    Gate0DryRunManifest,
    Gate0LiveExecutionAuthorization,
    Gate0LiveExecutionBlocked,
    Gate0Model,
    Gate0Observation,
    Gate0ProviderCallPlan,
    _artifact_text,
    _neutral_text,
    _priced_input_units,
    analyze_gate0_observations,
    build_gate0_provider_identity,
    validate_gate0_dry_run_conformance,
    write_gate0_json_atomic,
)


GATE0_RUN_SCHEMA_VERSION = "forced-exchange-gate0-run-v1"
GATE0_CALL_RESULT_SCHEMA_VERSION = "forced-exchange-gate0-call-result-v1"
GATE0_LIVE_ENABLE_ENV = "AGINTOR_ENABLE_LIVE_GATE0"

Gate0RunProvenance = Literal["deterministic_fixture", "authorized_live"]
Gate0ExecutionLiveStatus = Literal["not_run", "executed"]
Gate0CallInvalidStatus = Literal[
    "valid",
    "invalid_output",
    "provider_error",
    "accounting_error",
    "deadline_exceeded",
]


class Gate0CallUsage(Gate0Model):
    priced_input_units: int | None = Field(default=None, ge=0)
    output_units: int | None = Field(default=None, ge=0)
    cached_input_units: int | None = Field(default=None, ge=0)
    cache_write_input_units: int | None = Field(default=None, ge=0)
    known_cost_usd: float | None = Field(default=None, ge=0.0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    unknown_usage: bool = False
    unknown_cost: bool = False

    @classmethod
    def unknown(cls) -> "Gate0CallUsage":
        return cls(unknown_usage=True, unknown_cost=True)

    @model_validator(mode="after")
    def validate_usage(self) -> "Gate0CallUsage":
        usage_values = (
            self.priced_input_units,
            self.output_units,
            self.cached_input_units,
            self.cache_write_input_units,
        )
        if self.unknown_usage:
            if any(value is not None for value in usage_values):
                raise ValueError("unknown Gate0 usage must not fabricate numeric token counts")
        else:
            if self.priced_input_units is None or self.output_units is None:
                raise ValueError("known Gate0 usage requires priced_input_units and output_units")
            if self.cached_input_units is None:
                object.__setattr__(self, "cached_input_units", 0)
            if self.cache_write_input_units is None:
                object.__setattr__(self, "cache_write_input_units", 0)
            if self.cached_input_units + self.cache_write_input_units > self.priced_input_units:
                raise ValueError("Gate0 cached and cache-write input units exceed priced input units")
        if self.unknown_cost:
            if self.known_cost_usd is not None or self.estimated_cost_usd is not None:
                raise ValueError("unknown Gate0 cost must not fabricate numeric dollars")
        else:
            if self.known_cost_usd is None:
                object.__setattr__(self, "known_cost_usd", 0.0)
            if self.estimated_cost_usd is None:
                object.__setattr__(self, "estimated_cost_usd", 0.0)
        return self


class Gate0CallExecutionResult(Gate0Model):
    schema_version: Literal[GATE0_CALL_RESULT_SCHEMA_VERSION] = GATE0_CALL_RESULT_SCHEMA_VERSION
    call_id: str
    request_digest: str
    context_digest: str
    pair_key_digest: str
    provider_config_digest: str
    request_sent: bool
    response_id: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    output_digest: str = ""
    usage: Gate0CallUsage | None = None
    latency_ms: int = Field(ge=0)
    invalid_status: Gate0CallInvalidStatus = "valid"
    failure_detail: str | None = None
    result_digest: str = ""

    @field_validator(
        "call_id",
        "request_digest",
        "context_digest",
        "pair_key_digest",
        "provider_config_digest",
    )
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{info.field_name} may not be empty")
        return normalized

    @field_validator("response_id")
    @classmethod
    def validate_response_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized or len(normalized) > 256 or any(character.isspace() for character in normalized):
            raise ValueError("Gate0 response_id must be a non-secret opaque identifier")
        return normalized

    @model_validator(mode="after")
    def validate_result(self) -> "Gate0CallExecutionResult":
        analyzable = self.invalid_status in {"valid", "invalid_output"}
        if analyzable and (not self.request_sent or not self.response_id or self.usage is None):
            raise ValueError("analyzable Gate0 call results require sent request, response id, and usage")
        if self.invalid_status == "valid" and not self.output:
            raise ValueError("valid Gate0 call result requires typed output")
        if self.invalid_status == "deadline_exceeded":
            if not self.request_sent:
                raise ValueError("deadline-exceeded Gate0 calls must be treated as sent for conservative accounting")
            if self.response_id is not None or self.output:
                raise ValueError("deadline-exceeded Gate0 calls cannot claim a late response")
            if self.usage is None or not self.usage.unknown_usage or not self.usage.unknown_cost:
                raise ValueError("deadline-exceeded Gate0 calls require unknown usage and cost")
        if not analyzable and not self.failure_detail:
            raise ValueError("failed Gate0 call result requires failure_detail")
        output_digest = evidence_digest({"kind": "gate0-call-output", "output": self.output})
        if self.output_digest and self.output_digest != output_digest:
            raise ValueError("Gate0 call output_digest mismatch")
        if not self.output_digest:
            object.__setattr__(self, "output_digest", output_digest)
        payload = self.model_dump(mode="python", exclude={"result_digest"})
        computed = evidence_digest({"kind": GATE0_CALL_RESULT_SCHEMA_VERSION, **payload})
        if self.result_digest and self.result_digest != computed:
            raise ValueError("Gate0 call result_digest mismatch")
        if not self.result_digest:
            object.__setattr__(self, "result_digest", computed)
        return self


@runtime_checkable
class Gate0CallExecutor(Protocol):
    """Provider-neutral boundary: exactly one call plan in, one typed result out."""

    def execute(
        self,
        call_plan: Gate0ProviderCallPlan,
        *,
        deployment_profile: HarnessDeploymentProfile | None,
        credential_reference: CredentialReference | None,
    ) -> Gate0CallExecutionResult: ...


class Gate0RawCallObservation(Gate0Model):
    sequence_index: int = Field(ge=0)
    manifest_digest: str
    provenance: Gate0RunProvenance
    scheduled_call_id: str
    scheduled_request_digest: str
    executed_call: Gate0ProviderCallPlan
    paired_producer_call_id: str | None = None
    result: Gate0CallExecutionResult
    observation_digest: str = ""

    @model_validator(mode="after")
    def validate_observation(self) -> "Gate0RawCallObservation":
        if self.scheduled_call_id != self.executed_call.call_id:
            raise ValueError("raw Gate0 call observation crossed scheduled call id")
        if self.result.call_id != self.executed_call.call_id:
            raise ValueError("raw Gate0 call result crossed executed call id")
        payload = self.model_dump(mode="python", exclude={"observation_digest"})
        computed = evidence_digest({"kind": "gate0-raw-call-observation-v1", **payload})
        if self.observation_digest and self.observation_digest != computed:
            raise ValueError("raw Gate0 call observation digest mismatch")
        if not self.observation_digest:
            object.__setattr__(self, "observation_digest", computed)
        return self


class Gate0RawArmObservation(Gate0Model):
    sample_id: str
    manifest_digest: str
    provenance: Gate0RunProvenance
    arm_plan_digest: str
    producer_call_observation_digest: str
    responder_call_observation_digest: str
    delivered_artifact_digest: str | None = None
    total_priced_input_units: int = Field(ge=0)
    total_known_cost_usd: float = Field(ge=0.0)
    total_estimated_cost_usd: float = Field(ge=0.0)
    observation: Gate0Observation
    arm_observation_digest: str = ""

    @model_validator(mode="after")
    def bind_digest(self) -> "Gate0RawArmObservation":
        payload = self.model_dump(mode="python", exclude={"arm_observation_digest"})
        computed = evidence_digest({"kind": "gate0-raw-arm-observation-v1", **payload})
        if self.arm_observation_digest and self.arm_observation_digest != computed:
            raise ValueError("raw Gate0 arm observation digest mismatch")
        if not self.arm_observation_digest:
            object.__setattr__(self, "arm_observation_digest", computed)
        return self


class Gate0RunFailure(Gate0Model):
    failure_code: Literal[
        "executor_error",
        "provider_error",
        "accounting_error",
        "deadline_exceeded",
        "crossed_identity",
        "duplicate_response_id",
        "artifact_identity_mismatch",
        "persistence_error",
    ]
    call_id: str | None = None
    sequence_index: int = Field(ge=0)
    detail: str


@dataclass(frozen=True, slots=True)
class _SupervisedCallOutcome:
    result: Gate0CallExecutionResult | None = None
    error: Exception | None = None


class Gate0ExecutionReport(Gate0Model):
    schema_version: Literal[GATE0_RUN_SCHEMA_VERSION] = GATE0_RUN_SCHEMA_VERSION
    execution_id: str
    execution_digest: str = ""
    manifest_digest: str
    authorization_digest: str | None = None
    profile_digest: str | None = None
    provenance: Gate0RunProvenance
    live_status: Gate0ExecutionLiveStatus
    status: Literal["completed", "incomplete"]
    scheduled_call_count: int = Field(gt=0)
    completed_call_count: int = Field(ge=0)
    completed_arm_count: int = Field(ge=0)
    observed_priced_input_units: int = Field(ge=0)
    total_known_cost_usd: float = Field(ge=0.0)
    total_estimated_cost_usd: float = Field(ge=0.0)
    call_observation_digests: tuple[str, ...]
    arm_observation_digests: tuple[str, ...]
    failure: Gate0RunFailure | None = None
    analysis: Gate0AnalysisReport | None = None
    unknown_usage_event_count: int = Field(default=0, ge=0)
    unknown_cost_event_count: int = Field(default=0, ge=0)
    numerical_gate_passed: bool

    @field_validator("authorization_digest", "profile_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def validate_report(self) -> "Gate0ExecutionReport":
        if self.provenance == "deterministic_fixture":
            if (
                self.live_status != "not_run"
                or self.authorization_digest is not None
                or self.profile_digest is not None
            ):
                raise ValueError("deterministic Gate0 fixtures cannot carry live authority")
        elif (
            self.live_status != "executed"
            or self.authorization_digest is None
            or self.profile_digest is None
        ):
            raise ValueError("authorized Gate0 reports require exact live authority digests")
        if self.status == "completed":
            if self.completed_call_count != self.scheduled_call_count or self.failure is not None:
                raise ValueError("completed Gate0 run must cover schedule without failure")
            if self.analysis is None or self.numerical_gate_passed != self.analysis.numerical_gate_passed:
                raise ValueError("completed Gate0 run requires its exact analysis")
            if self.analysis.live_status != self.live_status:
                raise ValueError("Gate0 analysis live status differs from execution provenance")
        else:
            if self.failure is None or self.analysis is not None or self.numerical_gate_passed:
                raise ValueError("incomplete Gate0 run must fail closed without analysis")
        payload = self.model_dump(mode="python", exclude={"execution_digest"})
        computed = evidence_digest({"kind": GATE0_RUN_SCHEMA_VERSION, **payload})
        if self.execution_digest and self.execution_digest != computed:
            raise ValueError("Gate0 execution report digest mismatch")
        if not self.execution_digest:
            object.__setattr__(self, "execution_digest", computed)
        return self


def run_gate0_fixture(
    *,
    manifest: Gate0DryRunManifest,
    executor: Gate0CallExecutor,
    evidence_root: str | Path,
    call_deadline_ms: int = 120_000,
) -> Gate0ExecutionReport:
    """Execute only a deterministic fixture; never changes the manifest live status."""

    return _run_gate0(
        manifest=manifest,
        executor=executor,
        evidence_root=evidence_root,
        provenance="deterministic_fixture",
        live_status="not_run",
        authorization_digest=None,
        deployment_profile=None,
        credential_reference=None,
        call_deadline_ms=call_deadline_ms,
    )


def run_gate0_live(
    *,
    manifest: Gate0DryRunManifest,
    executor: Gate0CallExecutor,
    evidence_root: str | Path,
    authorization: Gate0LiveExecutionAuthorization,
    live_execution_marker: Literal["live_gate0"],
    call_deadline_ms: int = 120_000,
) -> Gate0ExecutionReport:
    """LIVE-ONLY entrypoint. No provider implementation or fallback is supplied here."""

    if live_execution_marker != "live_gate0":
        raise Gate0LiveExecutionBlocked("Gate0 live runner requires the explicit live_gate0 marker")
    if os.environ.get(GATE0_LIVE_ENABLE_ENV, "").strip() != "1":
        raise Gate0LiveExecutionBlocked(
            f"Gate0 live runner requires {GATE0_LIVE_ENABLE_ENV}=1"
        )
    try:
        validated_authorization = Gate0LiveExecutionAuthorization.model_validate(
            authorization.model_dump(mode="python")
        )
    except Exception as exc:
        raise Gate0LiveExecutionBlocked(f"Gate0 live authorization is malformed: {exc}") from exc
    if (
        validated_authorization.manifest_digest != manifest.manifest_digest
        or validated_authorization.provider_identity != manifest.provider_identity
        or not validated_authorization.live_authorized
    ):
        raise Gate0LiveExecutionBlocked(
            "Gate0 live authorization crossed the dry-run manifest or provider identity"
        )
    return _run_gate0(
        manifest=manifest,
        executor=executor,
        evidence_root=evidence_root,
        provenance="authorized_live",
        live_status="executed",
        authorization_digest=validated_authorization.authorization_digest,
        deployment_profile=validated_authorization.deployment_profile,
        credential_reference=validated_authorization.credential_reference,
        call_deadline_ms=call_deadline_ms,
    )


run_gate0_live.live_gate0_only = True  # type: ignore[attr-defined]


def replay_gate0_run(
    *,
    manifest: Gate0DryRunManifest,
    evidence_root: str | Path,
) -> Gate0ExecutionReport:
    root = Path(evidence_root).resolve()
    report_path = root / "final_report.json"
    if not report_path.is_file():
        raise FileNotFoundError("Gate0 final report is missing")
    report = Gate0ExecutionReport.model_validate(json.loads(report_path.read_text(encoding="utf-8")))
    if report.manifest_digest != manifest.manifest_digest:
        raise ValueError("persisted Gate0 report crossed the supplied manifest")
    call_files = sorted((root / "calls").glob("*.json")) if (root / "calls").is_dir() else []
    arm_files = sorted((root / "arms").glob("*.json")) if (root / "arms").is_dir() else []
    calls = [
        Gate0RawCallObservation.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in call_files
    ]
    arms = [
        Gate0RawArmObservation.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in arm_files
    ]
    expected_calls = {
        call.call_id: call
        for arm in manifest.arms
        for call in arm.calls
    }
    if tuple(item.sequence_index for item in calls) != tuple(range(len(calls))):
        raise ValueError("persisted Gate0 call sequence is not contiguous")
    if tuple(item.scheduled_call_id for item in calls) != manifest.provider_call_schedule[: len(calls)]:
        raise ValueError("persisted Gate0 calls do not follow the frozen schedule")
    response_ids: list[str] = []
    for item in calls:
        scheduled = expected_calls.get(item.scheduled_call_id)
        if scheduled is None or item.scheduled_request_digest != scheduled.request_digest:
            raise ValueError("persisted Gate0 call crossed the frozen request")
        crossed = _crossed_result_identity(item.result, item.executed_call, manifest)
        if crossed:
            raise ValueError("persisted Gate0 call result " + crossed)
        if item.result.response_id:
            response_ids.append(item.result.response_id)
    duplicate_failure_recorded = bool(
        report.status == "incomplete"
        and report.failure is not None
        and report.failure.failure_code == "duplicate_response_id"
    )
    if len(response_ids) != len(set(response_ids)) and not duplicate_failure_recorded:
        raise ValueError("persisted Gate0 response IDs are not unique")
    if tuple(item.observation_digest for item in calls) != report.call_observation_digests:
        raise ValueError("persisted Gate0 call observations differ from final report")
    if tuple(item.arm_observation_digest for item in arms) != report.arm_observation_digests:
        raise ValueError("persisted Gate0 arm observations differ from final report")
    if report.status == "completed":
        if report.scheduled_call_count != manifest.total_provider_calls:
            raise ValueError("persisted Gate0 scheduled call count crossed the manifest")
        if report.completed_arm_count != len(manifest.arms):
            raise ValueError("persisted Gate0 completed arm count crossed the manifest")
        if report.observed_priced_input_units != manifest.total_priced_input_units:
            raise ValueError("persisted Gate0 priced input total crossed the manifest")
        analysis = analyze_gate0_observations(
            manifest=manifest,
            observations=tuple(item.observation for item in arms),
        )
        if analysis != report.analysis:
            raise ValueError("replayed Gate0 analysis differs from persisted final report")
    elif not (root / "partial_run.json").is_file():
        raise FileNotFoundError("incomplete Gate0 run is missing partial_run.json")
    return report


def _run_gate0(
    *,
    manifest: Gate0DryRunManifest,
    executor: Gate0CallExecutor,
    evidence_root: str | Path,
    provenance: Gate0RunProvenance,
    live_status: Gate0ExecutionLiveStatus,
    authorization_digest: str | None,
    deployment_profile: HarnessDeploymentProfile | None,
    credential_reference: CredentialReference | None,
    call_deadline_ms: int,
) -> Gate0ExecutionReport:
    if call_deadline_ms <= 0:
        raise ValueError("Gate0 call_deadline_ms must be positive")
    if provenance == "deterministic_fixture":
        if (
            live_status != "not_run"
            or authorization_digest is not None
            or deployment_profile is not None
            or credential_reference is not None
        ):
            raise ValueError("Gate0 fixture execution cannot carry live authority")
    else:
        if (
            live_status != "executed"
            or authorization_digest is None
            or deployment_profile is None
            or credential_reference is None
        ):
            raise ValueError("authorized Gate0 execution requires its frozen live authority")
        if manifest.provider_identity != build_gate0_provider_identity(
            deployment_profile=deployment_profile
        ):
            raise ValueError("Gate0 execution profile crossed the manifest provider identity")
        if (
            credential_reference.provider_name != deployment_profile.provider
            or credential_reference.api_key_env
            != deployment_profile.endpoint.api_key_env
            or credential_reference.api_key_file_env
            != deployment_profile.endpoint.api_key_file_env
        ):
            raise ValueError("Gate0 execution credential crossed the frozen endpoint policy")
    conformance = validate_gate0_dry_run_conformance(manifest)
    if not conformance.passed:
        raise ValueError("Gate0 dry-run manifest failed deterministic conformance")
    destination = Path(evidence_root).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Gate0 evidence root already exists; runs are resumeless: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.gate0-{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        report = _execute_schedule(
            manifest=manifest,
            executor=executor,
            staging=staging,
            provenance=provenance,
            live_status=live_status,
            authorization_digest=authorization_digest,
            deployment_profile=deployment_profile,
            credential_reference=credential_reference,
            call_deadline_ms=call_deadline_ms,
        )
        if report.status == "incomplete":
            write_gate0_json_atomic(staging / "partial_run.json", report)
        write_gate0_json_atomic(staging / "final_report.json", report)
        staging.replace(destination)
        return replay_gate0_run(manifest=manifest, evidence_root=destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _execute_schedule(
    *,
    manifest: Gate0DryRunManifest,
    executor: Gate0CallExecutor,
    staging: Path,
    provenance: Gate0RunProvenance,
    live_status: Gate0ExecutionLiveStatus,
    authorization_digest: str | None,
    deployment_profile: HarnessDeploymentProfile | None,
    credential_reference: CredentialReference | None,
    call_deadline_ms: int,
) -> Gate0ExecutionReport:
    calls_by_id = {
        call.call_id: call
        for arm in manifest.arms
        for call in arm.calls
    }
    arms_by_sample = {arm.sample_id: arm for arm in manifest.arms}
    item_by_id = {item.item_id: item for item in manifest.panel.items}
    producer_artifacts: dict[str, tuple[str, str]] = {}
    call_observations: list[Gate0RawCallObservation] = []
    arm_observations: list[Gate0RawArmObservation] = []
    call_observation_by_id: dict[str, Gate0RawCallObservation] = {}
    response_ids: set[str] = set()
    failure: Gate0RunFailure | None = None

    for sequence_index, call_id in enumerate(manifest.provider_call_schedule):
        scheduled = calls_by_id.get(call_id)
        if scheduled is None:
            failure = _failure("crossed_identity", call_id, sequence_index, "scheduled call is absent")
            break
        arm = arms_by_sample[scheduled.sample_id]
        paired_producer_id: str | None = None
        effective = scheduled
        if scheduled.actor_id == "responder_b":
            producer = producer_artifacts.get(scheduled.sample_id)
            if producer is None:
                failure = _failure(
                    "artifact_identity_mismatch",
                    call_id,
                    sequence_index,
                    "paired producer artifact is missing",
                )
                break
            paired_producer_id, artifact_text = producer
            effective = _effective_responder_call(scheduled, artifact_text)
            if (
                effective.request_digest != scheduled.request_digest
                or effective.context_digest != scheduled.context_digest
                or effective.priced_input_units != scheduled.priced_input_units
            ):
                failure = _failure(
                    "artifact_identity_mismatch",
                    call_id,
                    sequence_index,
                    "paired producer artifact changed preregistered request identity or price",
                )
                break
        outcome = _execute_call_with_deadline(
            executor=executor,
            call_plan=effective,
            deployment_profile=deployment_profile,
            credential_reference=credential_reference,
            call_deadline_ms=call_deadline_ms,
        )
        if outcome.error is not None:
            exc = outcome.error
            failure = _failure("executor_error", call_id, sequence_index, str(exc))
            break
        if outcome.result is None:
            failure = _failure("executor_error", call_id, sequence_index, "executor produced no result")
            break
        result = outcome.result
        if result.invalid_status != "deadline_exceeded" and result.latency_ms > call_deadline_ms:
            result = _deadline_result(
                effective,
                max(result.latency_ms, call_deadline_ms),
                call_deadline_ms,
            )
        crossed = _crossed_result_identity(result, effective, manifest)
        if crossed:
            failure = _failure("crossed_identity", call_id, sequence_index, crossed)
            break
        observation = Gate0RawCallObservation(
            sequence_index=sequence_index,
            manifest_digest=manifest.manifest_digest,
            provenance=provenance,
            scheduled_call_id=scheduled.call_id,
            scheduled_request_digest=scheduled.request_digest,
            executed_call=effective,
            paired_producer_call_id=paired_producer_id,
            result=result,
        )
        call_observations.append(observation)
        call_observation_by_id[call_id] = observation
        _persist_call_observation(staging, observation)
        if result.response_id and result.response_id in response_ids:
            failure = _failure(
                "duplicate_response_id",
                call_id,
                sequence_index,
                f"duplicate response id {result.response_id!r}",
            )
            break
        if result.response_id:
            response_ids.add(result.response_id)
        if result.latency_ms > call_deadline_ms or result.invalid_status == "deadline_exceeded":
            failure = _failure("deadline_exceeded", call_id, sequence_index, result.failure_detail or "deadline exceeded")
            break
        if result.invalid_status == "provider_error":
            failure = _failure("provider_error", call_id, sequence_index, result.failure_detail or "provider error")
            break
        if result.invalid_status == "accounting_error":
            failure = _failure("accounting_error", call_id, sequence_index, result.failure_detail or "accounting error")
            break
        if result.usage is None or result.usage.unknown_usage or result.usage.unknown_cost:
            failure = _failure("accounting_error", call_id, sequence_index, "usage or cost is unknown")
            break
        if result.usage.priced_input_units != effective.priced_input_units:
            failure = _failure(
                "accounting_error",
                call_id,
                sequence_index,
                "observed priced input differs from preregistered call",
            )
            break
        if scheduled.actor_id == "producer_a":
            artifact_text = result.output.get("artifact_text")
            if result.invalid_status != "valid" or not isinstance(artifact_text, str) or not artifact_text:
                failure = _failure(
                    "artifact_identity_mismatch",
                    call_id,
                    sequence_index,
                    "producer did not return one valid artifact_text",
                )
                break
            producer_artifacts[scheduled.sample_id] = (scheduled.call_id, artifact_text)
            continue

        producer_call = arm.calls[0]
        producer_observation = call_observation_by_id[producer_call.call_id]
        delivered = _delivered_artifact_text(effective)
        answer = result.output.get("answer")
        hard_invalid = result.invalid_status == "invalid_output" or not isinstance(answer, str)
        item = item_by_id[arm.item_id]
        observation_result = Gate0Observation(
            observation_id=f"{provenance}:{arm.sample_id}",
            manifest_digest=manifest.manifest_digest,
            item_id=arm.item_id,
            template_id=arm.template_id,
            replicate_index=arm.replicate_index,
            arm=arm.arm,
            pair_key=arm.pair_key,
            pair_key_digest=arm.pair_key_digest,
            provider_config_digest=manifest.provider_identity.provider_config_digest,
            source_kind="deterministic_fixture" if provenance == "deterministic_fixture" else "provider_result",
            hard_invalid=hard_invalid,
            correct_answer=bool(not hard_invalid and answer == item.expected_answer),
        )
        arm_observation = Gate0RawArmObservation(
            sample_id=arm.sample_id,
            manifest_digest=manifest.manifest_digest,
            provenance=provenance,
            arm_plan_digest=arm.sample_digest,
            producer_call_observation_digest=producer_observation.observation_digest,
            responder_call_observation_digest=observation.observation_digest,
            delivered_artifact_digest=(
                evidence_digest({"kind": "gate0-delivered-artifact", "text": delivered})
                if delivered is not None
                else None
            ),
            total_priced_input_units=(
                producer_observation.result.usage.priced_input_units
                + result.usage.priced_input_units
            ),
            total_known_cost_usd=(
                producer_observation.result.usage.known_cost_usd
                + result.usage.known_cost_usd
            ),
            total_estimated_cost_usd=(
                producer_observation.result.usage.known_cost_usd
                + producer_observation.result.usage.estimated_cost_usd
                + result.usage.known_cost_usd
                + result.usage.estimated_cost_usd
            ),
            observation=observation_result,
        )
        if arm_observation.total_priced_input_units != arm.total_priced_input_units:
            failure = _failure(
                "accounting_error",
                call_id,
                sequence_index,
                "arm priced input does not reconcile with preregistered total",
            )
            break
        arm_observations.append(arm_observation)
        _persist_arm_observation(staging, arm, arm_observation)

    completed = failure is None and len(call_observations) == manifest.total_provider_calls
    analysis: Gate0AnalysisReport | None = None
    if completed:
        if len(arm_observations) != len(manifest.arms):
            failure = _failure(
                "crossed_identity",
                None,
                len(call_observations),
                "completed call schedule did not produce every arm observation",
            )
            completed = False
        else:
            analysis = analyze_gate0_observations(
                manifest=manifest,
                observations=tuple(item.observation for item in arm_observations),
            )
    usage_rows = [item.result.usage for item in call_observations if item.result.usage is not None]
    known_usage_rows = [row for row in usage_rows if not row.unknown_usage]
    known_cost_rows = [row for row in usage_rows if not row.unknown_cost]
    profile_digest = (
        harness_deployment_profile_digest(deployment_profile)
        if deployment_profile is not None
        else None
    )
    execution_id = "gate0-run." + evidence_digest(
        {
            "manifest": manifest.manifest_digest,
            "provenance": provenance,
            "authorization": authorization_digest,
            "profile": profile_digest,
            "calls": [item.observation_digest for item in call_observations],
            "arms": [item.arm_observation_digest for item in arm_observations],
            "failure": failure.model_dump(mode="json") if failure else None,
        }
    )[:24]
    return Gate0ExecutionReport(
        execution_id=execution_id,
        manifest_digest=manifest.manifest_digest,
        authorization_digest=authorization_digest,
        profile_digest=profile_digest,
        provenance=provenance,
        live_status=live_status,
        status="completed" if completed else "incomplete",
        scheduled_call_count=manifest.total_provider_calls,
        completed_call_count=len(call_observations),
        completed_arm_count=len(arm_observations),
        observed_priced_input_units=sum(row.priced_input_units or 0 for row in known_usage_rows),
        total_known_cost_usd=sum(row.known_cost_usd or 0.0 for row in known_cost_rows),
        total_estimated_cost_usd=sum(
            (row.known_cost_usd or 0.0) + (row.estimated_cost_usd or 0.0)
            for row in known_cost_rows
        ),
        call_observation_digests=tuple(item.observation_digest for item in call_observations),
        arm_observation_digests=tuple(item.arm_observation_digest for item in arm_observations),
        failure=failure,
        analysis=analysis,
        unknown_usage_event_count=sum(1 for row in usage_rows if row.unknown_usage),
        unknown_cost_event_count=sum(1 for row in usage_rows if row.unknown_cost),
        numerical_gate_passed=bool(analysis and analysis.numerical_gate_passed),
    )


def _effective_responder_call(
    scheduled: Gate0ProviderCallPlan,
    producer_artifact: str,
) -> Gate0ProviderCallPlan:
    payload = json.loads(json.dumps(scheduled.request_payload))
    if scheduled.arm in {"intact_exchange", "private_a_only"}:
        payload["delivered_artifact"]["artifact_text"] = producer_artifact
    elif scheduled.arm == "matched_neutral_artifact":
        payload["delivered_artifact"]["artifact_text"] = _neutral_text(producer_artifact)
    model_payload = scheduled.model_dump(mode="python")
    model_payload.update(
        {
            "request_payload": payload,
            "request_digest": "",
            "context_digest": "",
            "input_character_count": 0,
            "priced_input_units": 0,
        }
    )
    return Gate0ProviderCallPlan.model_validate(model_payload)


def _crossed_result_identity(
    result: Gate0CallExecutionResult,
    call: Gate0ProviderCallPlan,
    manifest: Gate0DryRunManifest,
) -> str:
    expected = {
        "call_id": call.call_id,
        "request_digest": call.request_digest,
        "context_digest": call.context_digest,
        "pair_key_digest": call.pair_key_digest,
        "provider_config_digest": manifest.provider_identity.provider_config_digest,
    }
    crossed = [name for name, value in expected.items() if getattr(result, name) != value]
    return "result crossed " + ", ".join(crossed) if crossed else ""


def _delivered_artifact_text(call: Gate0ProviderCallPlan) -> str | None:
    artifact = call.request_payload.get("delivered_artifact")
    if not isinstance(artifact, Mapping):
        return None
    text = artifact.get("artifact_text")
    return str(text) if isinstance(text, str) else None


def _failure(
    code: str,
    call_id: str | None,
    sequence_index: int,
    detail: str,
) -> Gate0RunFailure:
    return Gate0RunFailure(
        failure_code=code,
        call_id=call_id,
        sequence_index=max(sequence_index, 0),
        detail=str(detail or code)[:2000],
    )


def _execute_call_with_deadline(
    *,
    executor: Gate0CallExecutor,
    call_plan: Gate0ProviderCallPlan,
    deployment_profile: HarnessDeploymentProfile | None,
    credential_reference: CredentialReference | None,
    call_deadline_ms: int,
) -> _SupervisedCallOutcome:
    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    deadline_s = call_deadline_ms / 1000.0
    started_at = time.monotonic()

    def put_once(kind: str, payload: Any) -> None:
        try:
            results.put_nowait((kind, payload))
        except queue.Full:
            pass

    def invoke_executor() -> None:
        try:
            raw_result = executor.execute(
                call_plan,
                deployment_profile=deployment_profile,
                credential_reference=credential_reference,
            )
            put_once("result", Gate0CallExecutionResult.model_validate(raw_result))
        except Exception as exc:
            put_once("error", exc)

    worker = threading.Thread(
        target=invoke_executor,
        name=f"agintor-gate0-{evidence_digest(call_plan.call_id)[:16]}",
        daemon=True,
    )
    worker.start()

    try:
        kind, payload = results.get(timeout=deadline_s)
    except queue.Empty:
        _request_executor_cancellation(executor, call_plan.call_id)
        return _SupervisedCallOutcome(
            result=_deadline_result(
                call_plan,
                int(max(call_deadline_ms, (time.monotonic() - started_at) * 1000.0)),
                call_deadline_ms,
            )
        )

    if time.monotonic() - started_at > deadline_s:
        _request_executor_cancellation(executor, call_plan.call_id)
        return _SupervisedCallOutcome(
            result=_deadline_result(
                call_plan,
                int(max(call_deadline_ms, (time.monotonic() - started_at) * 1000.0)),
                call_deadline_ms,
            )
        )
    if kind == "error":
        return _SupervisedCallOutcome(error=payload)
    return _SupervisedCallOutcome(result=payload)


def _request_executor_cancellation(executor: Gate0CallExecutor, call_id: str) -> None:
    cancel = getattr(executor, "cancel", None)
    if not callable(cancel):
        return
    try:
        cancel(call_id)
    except Exception:
        pass


def _deadline_result(
    call_plan: Gate0ProviderCallPlan,
    elapsed_ms: int,
    call_deadline_ms: int,
) -> Gate0CallExecutionResult:
    return Gate0CallExecutionResult(
        call_id=call_plan.call_id,
        request_digest=call_plan.request_digest,
        context_digest=call_plan.context_digest,
        pair_key_digest=call_plan.pair_key_digest,
        provider_config_digest=call_plan.pair_key.provider_config_digest,
        request_sent=True,
        output={},
        usage=Gate0CallUsage.unknown(),
        latency_ms=elapsed_ms,
        invalid_status="deadline_exceeded",
        failure_detail=f"call exceeded {call_deadline_ms}ms deadline; usage and cost are unknown",
    )


def _persist_call_observation(staging: Path, observation: Gate0RawCallObservation) -> None:
    name = f"{observation.sequence_index:04d}-{evidence_digest(observation.scheduled_call_id)[:16]}.json"
    write_gate0_json_atomic(staging / "calls" / name, observation)


def _persist_arm_observation(
    staging: Path,
    arm: Gate0ArmPlan,
    observation: Gate0RawArmObservation,
) -> None:
    name = f"{arm.randomization_rank:020d}-{evidence_digest(arm.sample_id)[:16]}.json"
    write_gate0_json_atomic(staging / "arms" / name, observation)


__all__ = [
    "GATE0_LIVE_ENABLE_ENV",
    "GATE0_RUN_SCHEMA_VERSION",
    "Gate0CallExecutionResult",
    "Gate0CallExecutor",
    "Gate0CallUsage",
    "Gate0ExecutionReport",
    "Gate0RawArmObservation",
    "Gate0RawCallObservation",
    "Gate0RunFailure",
    "replay_gate0_run",
    "run_gate0_fixture",
    "run_gate0_live",
]
