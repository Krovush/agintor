from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...contracts.epochs import TaskCeilings


class CostStatus(str, Enum):
    KNOWN = "known"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class UsageStatus(str, Enum):
    KNOWN = "known"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ReservationKind(str, Enum):
    PROVIDER = "provider"
    TOOL = "tool"


class ProviderUsageReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    usage_status: UsageStatus
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cost_status: CostStatus
    cost_usd: float | None = Field(default=None, ge=0.0)
    response_id: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> "ProviderUsageReport":
        if self.usage_status is not UsageStatus.UNKNOWN and self.cache_write_tokens is None:
            object.__setattr__(self, "cache_write_tokens", 0)
        token_values = (
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.cache_write_tokens,
        )
        if self.usage_status is not UsageStatus.UNKNOWN and any(
            value is None for value in token_values
        ):
            raise ValueError("known or estimated usage requires every token count")
        if (
            self.usage_status is not UsageStatus.UNKNOWN
            and self.input_tokens is not None
            and self.cached_tokens is not None
            and self.cache_write_tokens is not None
            and self.cached_tokens + self.cache_write_tokens > self.input_tokens
        ):
            raise ValueError("cached and cache-write tokens must be input-token subcategories")
        if self.cost_status is CostStatus.UNKNOWN and self.cost_usd is not None:
            raise ValueError("unknown cost must not be represented by a numeric value")
        if self.cost_status is not CostStatus.UNKNOWN and self.cost_usd is None:
            raise ValueError("known or estimated cost requires cost_usd")
        return self

    @classmethod
    def unknown(cls) -> "ProviderUsageReport":
        return cls(
            usage_status=UsageStatus.UNKNOWN,
            cost_status=CostStatus.UNKNOWN,
        )


class BudgetReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    reservation_id: str
    kind: ReservationKind
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    expected_cost_status: CostStatus | None = None
    tool_calls: int = Field(default=0, ge=0)
    tool_output_bytes: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)


class AggregateBudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    known_cost_usd: float = Field(ge=0.0)
    estimated_cost_usd: float = Field(ge=0.0)
    unknown_cost_events: int = Field(ge=0)
    unknown_usage_events: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_output_bytes: int = Field(ge=0)
    retries: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0)
    elapsed_wall_time_ms: int = Field(ge=0)
    remaining_wall_time_ms: int = Field(ge=0)
    deadline_exceeded: bool
    active_reservations: int = Field(ge=0)
    violations: tuple[str, ...] = ()
    healthy: bool
    reconciled: bool
    promotion_eligible: bool


class BudgetExhaustedError(RuntimeError):
    def __init__(
        self,
        metric: str,
        *,
        requested: int | float = 0,
        remaining: int | float = 0,
    ) -> None:
        self.metric = metric
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"aggregate budget exhausted for {metric}: requested={requested} remaining={remaining}"
        )


@dataclass(slots=True)
class _Totals:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    known_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    unknown_cost_events: int = 0
    unknown_usage_events: int = 0
    tool_calls: int = 0
    tool_output_bytes: int = 0
    retries: int = 0
    latency_ms: float = 0.0


class AggregateBudgetLedger:
    """Thread-safe aggregate budget with reserve-before-dispatch semantics."""

    def __init__(
        self,
        ceilings: TaskCeilings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ceilings = ceilings
        self._clock = clock
        self._started_at = float(clock())
        self._lock = threading.RLock()
        self._totals = _Totals()
        self._reservations: dict[str, BudgetReservation] = {}
        self._sequence = 0
        self._violations: set[str] = set()

    def _elapsed_ms_unlocked(self) -> float:
        return max(0.0, (float(self._clock()) - self._started_at) * 1000.0)

    def remaining_wall_time_ms(self) -> int:
        with self._lock:
            remaining = self.ceilings.max_wall_time_ms - self._elapsed_ms_unlocked()
            return max(0, int(remaining))

    def provider_timeout_ms(self) -> int:
        return min(self.ceilings.provider_deadline_ms, self.remaining_wall_time_ms())

    def remaining_tool_output_bytes(self) -> int:
        with self._lock:
            remaining = (
                self.ceilings.max_tool_output_bytes
                - self._totals.tool_output_bytes
                - int(self._reserved_unlocked("tool_output_bytes"))
            )
            return max(0, int(remaining))

    def _require_time_unlocked(self) -> None:
        remaining = self.ceilings.max_wall_time_ms - self._elapsed_ms_unlocked()
        if remaining <= 0:
            raise BudgetExhaustedError("wall_time_ms", requested=1, remaining=0)

    @staticmethod
    def _require_nonnegative(name: str, value: int | float) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if value < 0:
            raise ValueError(f"{name} may not be negative")

    @staticmethod
    def _require_capacity(
        metric: str,
        *,
        current: int | float,
        reserved: int | float,
        requested: int | float,
        limit: int | float,
    ) -> None:
        remaining = max(0, limit - current - reserved)
        if requested > remaining + 1e-12:
            raise BudgetExhaustedError(metric, requested=requested, remaining=remaining)

    def _reserved_unlocked(self, field_name: str) -> int | float:
        return sum(getattr(reservation, field_name) for reservation in self._reservations.values())

    def _next_reservation_id_unlocked(self, kind: ReservationKind) -> str:
        self._sequence += 1
        return f"budget.{kind.value}.{self._sequence:06d}"

    def reserve_provider_call(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        max_cached_tokens: int = 0,
        max_cache_write_tokens: int = 0,
        estimated_cost_usd: float,
        expected_cost_status: CostStatus = CostStatus.KNOWN,
        is_retry: bool = False,
    ) -> BudgetReservation:
        expected_cost_status = CostStatus(expected_cost_status)
        values = {
            "input_tokens": input_tokens,
            "max_output_tokens": max_output_tokens,
            "max_cached_tokens": max_cached_tokens,
            "max_cache_write_tokens": max_cache_write_tokens,
            "estimated_cost_usd": estimated_cost_usd,
        }
        for name, value in values.items():
            self._require_nonnegative(name, value)
        if max_cached_tokens > input_tokens or max_cache_write_tokens > input_tokens:
            raise ValueError("cache reservations must not exceed input tokens")
        if expected_cost_status is CostStatus.UNKNOWN:
            raise ValueError("provider calls require a bounded known or estimated cost reservation")
        with self._lock:
            self._require_time_unlocked()
            if self._totals.unknown_cost_events or self._totals.unknown_usage_events:
                raise BudgetExhaustedError("provider_accounting_unknown", requested=1, remaining=0)
            if self._violations:
                raise BudgetExhaustedError("ledger_accounting_invalid", requested=1, remaining=0)
            requested_retry = int(bool(is_retry))
            checks = (
                ("model_calls", self._totals.model_calls, 1, self.ceilings.max_model_calls),
                ("input_tokens", self._totals.input_tokens, input_tokens, self.ceilings.max_input_tokens),
                ("output_tokens", self._totals.output_tokens, max_output_tokens, self.ceilings.max_output_tokens),
                ("cached_tokens", self._totals.cached_tokens, max_cached_tokens, self.ceilings.max_cached_tokens),
                (
                    "cache_write_tokens",
                    self._totals.cache_write_tokens,
                    max_cache_write_tokens,
                    self.ceilings.max_cache_write_tokens,
                ),
                ("retries", self._totals.retries, requested_retry, self.ceilings.max_retries),
            )
            for metric, current, requested, limit in checks:
                self._require_capacity(
                    metric,
                    current=current,
                    reserved=self._reserved_unlocked(metric),
                    requested=requested,
                    limit=limit,
                )
            committed_cost = self._totals.known_cost_usd + self._totals.estimated_cost_usd
            reserved_cost = self._reserved_unlocked("estimated_cost_usd")
            self._require_capacity(
                "estimated_cost_usd",
                current=committed_cost,
                reserved=reserved_cost,
                requested=estimated_cost_usd,
                limit=self.ceilings.max_estimated_cost_usd,
            )
            if expected_cost_status is CostStatus.KNOWN:
                reserved_known_cost = sum(
                    reservation.estimated_cost_usd
                    for reservation in self._reservations.values()
                    if reservation.expected_cost_status is CostStatus.KNOWN
                )
                self._require_capacity(
                    "known_cost_usd",
                    current=self._totals.known_cost_usd,
                    reserved=reserved_known_cost,
                    requested=estimated_cost_usd,
                    limit=self.ceilings.max_known_cost_usd,
                )
            reservation = BudgetReservation(
                reservation_id=self._next_reservation_id_unlocked(ReservationKind.PROVIDER),
                kind=ReservationKind.PROVIDER,
                model_calls=1,
                input_tokens=input_tokens,
                output_tokens=max_output_tokens,
                cached_tokens=max_cached_tokens,
                cache_write_tokens=max_cache_write_tokens,
                estimated_cost_usd=estimated_cost_usd,
                expected_cost_status=expected_cost_status,
                retries=requested_retry,
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def reserve_tool_call(self, *, max_output_bytes: int) -> BudgetReservation:
        self._require_nonnegative("max_output_bytes", max_output_bytes)
        with self._lock:
            self._require_time_unlocked()
            if self._totals.unknown_cost_events or self._totals.unknown_usage_events:
                raise BudgetExhaustedError("ledger_accounting_unknown", requested=1, remaining=0)
            if self._violations:
                raise BudgetExhaustedError("ledger_accounting_invalid", requested=1, remaining=0)
            self._require_capacity(
                "tool_calls",
                current=self._totals.tool_calls,
                reserved=self._reserved_unlocked("tool_calls"),
                requested=1,
                limit=self.ceilings.max_tool_calls,
            )
            self._require_capacity(
                "tool_output_bytes",
                current=self._totals.tool_output_bytes,
                reserved=self._reserved_unlocked("tool_output_bytes"),
                requested=max_output_bytes,
                limit=self.ceilings.max_tool_output_bytes,
            )
            reservation = BudgetReservation(
                reservation_id=self._next_reservation_id_unlocked(ReservationKind.TOOL),
                kind=ReservationKind.TOOL,
                tool_calls=1,
                tool_output_bytes=max_output_bytes,
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def _pop_reservation_unlocked(
        self,
        reservation: BudgetReservation,
        expected_kind: ReservationKind,
    ) -> BudgetReservation:
        stored = self._reservations.get(reservation.reservation_id)
        if stored is None:
            raise ValueError(f"unknown or finalized budget reservation {reservation.reservation_id!r}")
        if stored != reservation or stored.kind is not expected_kind:
            raise ValueError(f"budget reservation {reservation.reservation_id!r} does not match ledger state")
        del self._reservations[reservation.reservation_id]
        return stored

    def cancel_reservation(self, reservation: BudgetReservation) -> AggregateBudgetSnapshot:
        with self._lock:
            self._pop_reservation_unlocked(reservation, reservation.kind)
            return self._snapshot_unlocked()

    def complete_provider_call(
        self,
        reservation: BudgetReservation,
        usage: ProviderUsageReport,
        *,
        latency_ms: float,
        request_sent: bool = True,
    ) -> AggregateBudgetSnapshot:
        self._require_nonnegative("latency_ms", latency_ms)
        if not request_sent and (
            usage.usage_status is not UsageStatus.UNKNOWN
            or usage.cost_status is not CostStatus.UNKNOWN
        ):
            raise ValueError("a pre-send provider result cannot report provider usage")
        with self._lock:
            stored = self._pop_reservation_unlocked(reservation, ReservationKind.PROVIDER)
            self._totals.retries += stored.retries
            self._totals.latency_ms += latency_ms
            if not request_sent:
                return self._snapshot_unlocked()

            self._totals.model_calls += 1
            observed_tokens = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
            }
            for field_name, observed in observed_tokens.items():
                if observed is not None:
                    setattr(self._totals, field_name, getattr(self._totals, field_name) + observed)
                    if observed > getattr(stored, field_name):
                        self._violations.add(f"{field_name}_reservation_exceeded")
            if usage.usage_status is UsageStatus.UNKNOWN:
                self._totals.unknown_usage_events += 1
            if usage.cost_status is CostStatus.KNOWN:
                self._totals.known_cost_usd += float(usage.cost_usd)
            elif usage.cost_status is CostStatus.ESTIMATED:
                self._totals.estimated_cost_usd += float(usage.cost_usd)
            else:
                self._totals.unknown_cost_events += 1
            if usage.cost_usd is not None and usage.cost_usd > stored.estimated_cost_usd + 1e-12:
                self._violations.add("cost_reservation_exceeded")
            self._record_ceiling_violations_unlocked()
            return self._snapshot_unlocked()

    def complete_provider_failure(
        self,
        reservation: BudgetReservation,
        *,
        request_sent: bool,
        usage: ProviderUsageReport | None,
        latency_ms: float,
    ) -> AggregateBudgetSnapshot:
        if request_sent:
            return self.complete_provider_call(
                reservation,
                usage or ProviderUsageReport.unknown(),
                latency_ms=latency_ms,
                request_sent=True,
            )
        return self.complete_provider_call(
            reservation,
            ProviderUsageReport.unknown(),
            latency_ms=latency_ms,
            request_sent=False,
        )

    def complete_tool_call(
        self,
        reservation: BudgetReservation,
        *,
        output_bytes: int,
        latency_ms: float = 0.0,
    ) -> AggregateBudgetSnapshot:
        self._require_nonnegative("output_bytes", output_bytes)
        self._require_nonnegative("latency_ms", latency_ms)
        with self._lock:
            stored = self._pop_reservation_unlocked(reservation, ReservationKind.TOOL)
            self._totals.tool_calls += 1
            self._totals.tool_output_bytes += output_bytes
            self._totals.latency_ms += latency_ms
            if output_bytes > stored.tool_output_bytes:
                self._violations.add("tool_output_bytes_reservation_exceeded")
            self._record_ceiling_violations_unlocked()
            return self._snapshot_unlocked()

    def _record_ceiling_violations_unlocked(self) -> None:
        checks = {
            "model_calls": (self._totals.model_calls, self.ceilings.max_model_calls),
            "input_tokens": (self._totals.input_tokens, self.ceilings.max_input_tokens),
            "output_tokens": (self._totals.output_tokens, self.ceilings.max_output_tokens),
            "cached_tokens": (self._totals.cached_tokens, self.ceilings.max_cached_tokens),
            "cache_write_tokens": (
                self._totals.cache_write_tokens,
                self.ceilings.max_cache_write_tokens,
            ),
            "tool_calls": (self._totals.tool_calls, self.ceilings.max_tool_calls),
            "tool_output_bytes": (self._totals.tool_output_bytes, self.ceilings.max_tool_output_bytes),
            "retries": (self._totals.retries, self.ceilings.max_retries),
            "known_cost_usd": (self._totals.known_cost_usd, self.ceilings.max_known_cost_usd),
            "estimated_cost_usd": (
                self._totals.known_cost_usd + self._totals.estimated_cost_usd,
                self.ceilings.max_estimated_cost_usd,
            ),
        }
        for metric, (observed, limit) in checks.items():
            if observed > limit + 1e-12:
                self._violations.add(f"{metric}_ceiling_exceeded")

    def snapshot(self) -> AggregateBudgetSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> AggregateBudgetSnapshot:
        elapsed_float = self._elapsed_ms_unlocked()
        elapsed = int(math.ceil(elapsed_float))
        remaining = max(0, int(self.ceilings.max_wall_time_ms - elapsed_float))
        deadline_exceeded = elapsed_float >= self.ceilings.max_wall_time_ms
        unknown = bool(self._totals.unknown_cost_events or self._totals.unknown_usage_events)
        healthy = not self._violations and not unknown
        reconciled = not self._reservations and not unknown
        return AggregateBudgetSnapshot(
            model_calls=self._totals.model_calls,
            input_tokens=self._totals.input_tokens,
            output_tokens=self._totals.output_tokens,
            cached_tokens=self._totals.cached_tokens,
            cache_write_tokens=self._totals.cache_write_tokens,
            known_cost_usd=self._totals.known_cost_usd,
            estimated_cost_usd=self._totals.estimated_cost_usd,
            unknown_cost_events=self._totals.unknown_cost_events,
            unknown_usage_events=self._totals.unknown_usage_events,
            tool_calls=self._totals.tool_calls,
            tool_output_bytes=self._totals.tool_output_bytes,
            retries=self._totals.retries,
            latency_ms=self._totals.latency_ms,
            elapsed_wall_time_ms=elapsed,
            remaining_wall_time_ms=remaining,
            deadline_exceeded=deadline_exceeded,
            active_reservations=len(self._reservations),
            violations=tuple(sorted(self._violations)),
            healthy=healthy,
            reconciled=reconciled,
            promotion_eligible=healthy and not deadline_exceeded,
        )


__all__ = [
    "AggregateBudgetLedger",
    "AggregateBudgetSnapshot",
    "BudgetExhaustedError",
    "BudgetReservation",
    "CostStatus",
    "ProviderUsageReport",
    "ReservationKind",
    "UsageStatus",
]
