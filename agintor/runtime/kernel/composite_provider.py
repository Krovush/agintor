from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .composite_budget import (
    AggregateBudgetLedger,
    AggregateBudgetSnapshot,
    BudgetExhaustedError,
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)


_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class CredentialReference(BaseModel):
    """Serializable credential locations; resolved secret values never enter this model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str = Field(min_length=1)
    api_key_env: str | None = None
    api_key_file_env: str | None = None

    @field_validator("provider_name")
    @classmethod
    def normalize_provider_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("provider_name may not be empty")
        return normalized

    @field_validator("api_key_env", "api_key_file_env")
    @classmethod
    def validate_environment_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _ENVIRONMENT_NAME_RE.fullmatch(normalized):
            raise ValueError("credential environment references must be environment variable names")
        return normalized

    @model_validator(mode="after")
    def require_reference(self) -> "CredentialReference":
        if not self.api_key_env and not self.api_key_file_env:
            raise ValueError("credential reference requires an API-key or key-file environment name")
        return self


class ProviderExecutionProvenance(BaseModel):
    """Adapter-declared provenance for public solve and no-live accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: Literal["deterministic_replay", "live_provider"]
    live_inference_status: Literal["not_run", "completed", "failed"]
    real_inference_requests_sent: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_provenance(self) -> "ProviderExecutionProvenance":
        if self.execution_mode == "deterministic_replay":
            if self.live_inference_status != "not_run":
                raise ValueError("deterministic replay cannot claim live inference status")
            if self.real_inference_requests_sent != 0:
                raise ValueError("deterministic replay cannot send real inference requests")
        elif self.live_inference_status == "not_run":
            if self.real_inference_requests_sent != 0:
                raise ValueError("not-run live inference cannot claim sent requests")
        elif self.real_inference_requests_sent == 0:
            raise ValueError("completed or failed live inference requires a sent request")
        return self


class ProviderCallStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class ProviderFailureKind(str, Enum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    PRE_SEND_FAILURE = "pre_send_failure"
    POST_SEND_FAILURE = "post_send_failure"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    INVALID_RESULT = "invalid_result"
    ACCOUNTING_INVALID = "accounting_invalid"


class ProviderInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    response: Any
    usage: ProviderUsageReport


class ProviderRequestReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)
    max_cached_tokens: int = Field(default=0, ge=0)
    max_cache_write_tokens: int = Field(default=0, ge=0)
    max_known_cost_usd: float | None = Field(default=None, ge=0.0)


class ProviderFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProviderFailureKind
    request_sent: bool
    usage_status: UsageStatus | None = None
    cost_status: CostStatus | None = None
    ambiguous_post_send: bool = False
    accounting_healthy: bool
    budget_metric: str | None = None
    error_type: str | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    provider_http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = None


class ProviderCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ProviderCallStatus
    reservation_id: str | None = None
    timeout_ms: int = Field(default=0, ge=0)
    invocation: ProviderInvocation | None = None
    failure: ProviderFailure | None = None
    ledger: AggregateBudgetSnapshot
    promotion_eligible: bool

    @model_validator(mode="after")
    def validate_result_shape(self) -> "ProviderCallResult":
        if self.status is ProviderCallStatus.SUCCEEDED:
            if self.invocation is None or self.failure is not None:
                raise ValueError("successful provider calls require an invocation and no failure")
            if self.promotion_eligible and not self.ledger.promotion_eligible:
                raise ValueError("provider call cannot override ledger promotion eligibility")
        elif self.failure is None or self.promotion_eligible:
            raise ValueError("non-successful provider calls require a failure and are not promotion eligible")
        return self


@dataclass(frozen=True, slots=True)
class ProviderCallControl:
    reservation_id: str
    timeout_ms: int
    deadline_monotonic: float
    cancellation_event: threading.Event

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline_monotonic - time.monotonic()) * 1000.0))

    @property
    def cancelled(self) -> bool:
        return self.cancellation_event.is_set()


class ControlledProvider(Protocol):
    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        ...


class ProviderInvocationError(RuntimeError):
    """Typed adapter failure with explicit send/accounting state."""

    def __init__(
        self,
        *,
        request_sent: bool,
        usage: ProviderUsageReport | None = None,
        cancelled: bool = False,
        deadline_exceeded: bool = False,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        if not request_sent and usage is not None:
            raise ValueError("pre-send failures cannot include provider usage")
        if cancelled and deadline_exceeded:
            raise ValueError("provider failure cannot be both cancelled and deadline-exceeded")
        self.request_sent = request_sent
        self.usage = usage
        self.cancelled = cancelled
        self.deadline_exceeded = deadline_exceeded
        self.provider_error_type = provider_error_type
        self.provider_error_code = provider_error_code
        self.provider_http_status = provider_http_status
        self.provider_request_id = provider_request_id
        super().__init__("controlled provider invocation failed")


class CompositeProviderController:
    """Dispatch controlled provider calls against one aggregate ledger."""

    def __init__(self, ledger: AggregateBudgetLedger) -> None:
        self.ledger = ledger

    @staticmethod
    def _failure(
        *,
        kind: ProviderFailureKind,
        request_sent: bool,
        usage: ProviderUsageReport | None,
        snapshot: AggregateBudgetSnapshot,
        ambiguous_post_send: bool = False,
        budget_metric: str | None = None,
        error_type: str | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> ProviderFailure:
        return ProviderFailure(
            kind=kind,
            request_sent=request_sent,
            usage_status=usage.usage_status if usage is not None else None,
            cost_status=usage.cost_status if usage is not None else None,
            ambiguous_post_send=ambiguous_post_send,
            accounting_healthy=snapshot.healthy,
            budget_metric=budget_metric,
            error_type=error_type,
            provider_error_type=provider_error_type,
            provider_error_code=provider_error_code,
            provider_http_status=provider_http_status,
            provider_request_id=provider_request_id,
        )

    @staticmethod
    def _request_cancellation(
        provider: ControlledProvider,
        control: ProviderCallControl,
    ) -> None:
        control.cancellation_event.set()
        cancel = getattr(provider, "cancel", None)
        if callable(cancel):
            try:
                cancel(control.reservation_id)
            except Exception:
                # Cancellation is already represented by the control event.  A
                # provider-specific cancellation error cannot make usage known.
                pass

    def call(
        self,
        provider: ControlledProvider,
        request: Any,
        *,
        input_tokens: int,
        max_output_tokens: int,
        max_cached_tokens: int = 0,
        max_cache_write_tokens: int = 0,
        estimated_cost_usd: float,
        expected_cost_status: CostStatus = CostStatus.KNOWN,
        is_retry: bool = False,
        credential_reference: CredentialReference | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> ProviderCallResult:
        if cancellation_event is not None and cancellation_event.is_set():
            snapshot = self.ledger.snapshot()
            failure = self._failure(
                kind=ProviderFailureKind.CANCELLED,
                request_sent=False,
                usage=None,
                snapshot=snapshot,
            )
            return ProviderCallResult(
                status=ProviderCallStatus.CANCELLED,
                failure=failure,
                ledger=snapshot,
                promotion_eligible=False,
            )
        try:
            reservation = self.ledger.reserve_provider_call(
                input_tokens=input_tokens,
                max_output_tokens=max_output_tokens,
                max_cached_tokens=max_cached_tokens,
                max_cache_write_tokens=max_cache_write_tokens,
                estimated_cost_usd=estimated_cost_usd,
                expected_cost_status=expected_cost_status,
                is_retry=is_retry,
            )
        except BudgetExhaustedError as exc:
            snapshot = self.ledger.snapshot()
            failure = self._failure(
                kind=ProviderFailureKind.BUDGET_EXHAUSTED,
                request_sent=False,
                usage=None,
                snapshot=snapshot,
                budget_metric=exc.metric,
            )
            return ProviderCallResult(
                status=ProviderCallStatus.REJECTED,
                failure=failure,
                ledger=snapshot,
                promotion_eligible=False,
            )

        timeout_ms = self.ledger.provider_timeout_ms()
        if timeout_ms <= 0:
            snapshot = self.ledger.cancel_reservation(reservation)
            failure = self._failure(
                kind=ProviderFailureKind.DEADLINE_EXCEEDED,
                request_sent=False,
                usage=None,
                snapshot=snapshot,
            )
            return ProviderCallResult(
                status=ProviderCallStatus.DEADLINE_EXCEEDED,
                reservation_id=reservation.reservation_id,
                failure=failure,
                ledger=snapshot,
                promotion_eligible=False,
            )

        started_at = time.monotonic()
        control = ProviderCallControl(
            reservation_id=reservation.reservation_id,
            timeout_ms=timeout_ms,
            deadline_monotonic=started_at + timeout_ms / 1000.0,
            cancellation_event=threading.Event(),
        )
        results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def invoke_provider() -> None:
            try:
                invocation = provider.invoke(
                    request,
                    control=control,
                    credential_reference=credential_reference,
                )
                results.put_nowait(("result", invocation))
            except Exception as exc:
                results.put_nowait(("error", exc))

        worker = threading.Thread(
            target=invoke_provider,
            name=f"agintor-{reservation.reservation_id}",
            daemon=True,
        )
        worker.start()

        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self._request_cancellation(provider, control)
                latency_ms = (time.monotonic() - started_at) * 1000.0
                snapshot = self.ledger.complete_provider_failure(
                    reservation,
                    request_sent=True,
                    usage=None,
                    latency_ms=latency_ms,
                )
                unknown = ProviderUsageReport.unknown()
                failure = self._failure(
                    kind=ProviderFailureKind.CANCELLED,
                    request_sent=True,
                    usage=unknown,
                    snapshot=snapshot,
                    ambiguous_post_send=True,
                )
                return ProviderCallResult(
                    status=ProviderCallStatus.CANCELLED,
                    reservation_id=reservation.reservation_id,
                    timeout_ms=timeout_ms,
                    failure=failure,
                    ledger=snapshot,
                    promotion_eligible=False,
                )
            remaining_s = control.deadline_monotonic - time.monotonic()
            if remaining_s <= 0:
                self._request_cancellation(provider, control)
                latency_ms = (time.monotonic() - started_at) * 1000.0
                snapshot = self.ledger.complete_provider_failure(
                    reservation,
                    request_sent=True,
                    usage=None,
                    latency_ms=latency_ms,
                )
                unknown = ProviderUsageReport.unknown()
                failure = self._failure(
                    kind=ProviderFailureKind.DEADLINE_EXCEEDED,
                    request_sent=True,
                    usage=unknown,
                    snapshot=snapshot,
                    ambiguous_post_send=True,
                )
                return ProviderCallResult(
                    status=ProviderCallStatus.DEADLINE_EXCEEDED,
                    reservation_id=reservation.reservation_id,
                    timeout_ms=timeout_ms,
                    failure=failure,
                    ledger=snapshot,
                    promotion_eligible=False,
                )
            try:
                result_kind, payload = results.get(timeout=min(remaining_s, 0.01))
                break
            except queue.Empty:
                continue

        latency_ms = (time.monotonic() - started_at) * 1000.0
        if result_kind == "result":
            try:
                invocation = ProviderInvocation.model_validate(payload)
            except Exception as exc:
                snapshot = self.ledger.complete_provider_failure(
                    reservation,
                    request_sent=True,
                    usage=None,
                    latency_ms=latency_ms,
                )
                unknown = ProviderUsageReport.unknown()
                failure = self._failure(
                    kind=ProviderFailureKind.INVALID_RESULT,
                    request_sent=True,
                    usage=unknown,
                    snapshot=snapshot,
                    ambiguous_post_send=True,
                    error_type=type(exc).__name__,
                )
                return ProviderCallResult(
                    status=ProviderCallStatus.FAILED,
                    reservation_id=reservation.reservation_id,
                    timeout_ms=timeout_ms,
                    failure=failure,
                    ledger=snapshot,
                    promotion_eligible=False,
                )
            snapshot = self.ledger.complete_provider_call(
                reservation,
                invocation.usage,
                latency_ms=latency_ms,
            )
            if snapshot.healthy and not snapshot.deadline_exceeded:
                return ProviderCallResult(
                    status=ProviderCallStatus.SUCCEEDED,
                    reservation_id=reservation.reservation_id,
                    timeout_ms=timeout_ms,
                    invocation=invocation,
                    ledger=snapshot,
                    promotion_eligible=snapshot.promotion_eligible,
                )
            failure = self._failure(
                kind=ProviderFailureKind.ACCOUNTING_INVALID,
                request_sent=True,
                usage=invocation.usage,
                snapshot=snapshot,
            )
            return ProviderCallResult(
                status=ProviderCallStatus.FAILED,
                reservation_id=reservation.reservation_id,
                timeout_ms=timeout_ms,
                invocation=invocation,
                failure=failure,
                ledger=snapshot,
                promotion_eligible=False,
            )

        error = payload
        if isinstance(error, ProviderInvocationError):
            usage = error.usage
            snapshot = self.ledger.complete_provider_failure(
                reservation,
                request_sent=error.request_sent,
                usage=usage,
                latency_ms=latency_ms,
            )
            ambiguous = bool(
                error.request_sent
                and (
                    usage is None
                    or usage.usage_status is UsageStatus.UNKNOWN
                    or usage.cost_status is CostStatus.UNKNOWN
                )
            )
            if error.cancelled:
                status = ProviderCallStatus.CANCELLED
                kind = ProviderFailureKind.CANCELLED
            elif error.deadline_exceeded:
                status = ProviderCallStatus.DEADLINE_EXCEEDED
                kind = ProviderFailureKind.DEADLINE_EXCEEDED
            elif error.request_sent:
                status = ProviderCallStatus.FAILED
                kind = ProviderFailureKind.POST_SEND_FAILURE
            else:
                status = ProviderCallStatus.FAILED
                kind = ProviderFailureKind.PRE_SEND_FAILURE
            effective_usage = usage or (ProviderUsageReport.unknown() if error.request_sent else None)
            failure = self._failure(
                kind=kind,
                request_sent=error.request_sent,
                usage=effective_usage,
                snapshot=snapshot,
                ambiguous_post_send=ambiguous,
                error_type=type(error).__name__,
                provider_error_type=error.provider_error_type,
                provider_error_code=error.provider_error_code,
                provider_http_status=error.provider_http_status,
                provider_request_id=error.provider_request_id,
            )
            return ProviderCallResult(
                status=status,
                reservation_id=reservation.reservation_id,
                timeout_ms=timeout_ms,
                failure=failure,
                ledger=snapshot,
                promotion_eligible=False,
            )

        snapshot = self.ledger.complete_provider_failure(
            reservation,
            request_sent=True,
            usage=None,
            latency_ms=latency_ms,
        )
        unknown = ProviderUsageReport.unknown()
        failure = self._failure(
            kind=ProviderFailureKind.POST_SEND_FAILURE,
            request_sent=True,
            usage=unknown,
            snapshot=snapshot,
            ambiguous_post_send=True,
            error_type=type(error).__name__,
        )
        return ProviderCallResult(
            status=ProviderCallStatus.FAILED,
            reservation_id=reservation.reservation_id,
            timeout_ms=timeout_ms,
            failure=failure,
            ledger=snapshot,
            promotion_eligible=False,
        )


__all__ = [
    "CompositeProviderController",
    "ControlledProvider",
    "CredentialReference",
    "ProviderCallControl",
    "ProviderCallResult",
    "ProviderCallStatus",
    "ProviderFailure",
    "ProviderFailureKind",
    "ProviderInvocation",
    "ProviderInvocationError",
    "ProviderExecutionProvenance",
    "ProviderRequestReservation",
]
