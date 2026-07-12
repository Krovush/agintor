from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import TaskCeilings
from agintor.runtime.kernel.composite_budget import (
    AggregateBudgetLedger,
    BudgetExhaustedError,
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import (
    CompositeProviderController,
    CredentialReference,
    ProviderCallControl,
    ProviderCallStatus,
    ProviderFailureKind,
    ProviderInvocation,
    ProviderInvocationError,
)


def _ceilings(**updates: Any) -> TaskCeilings:
    payload = {
        "max_model_calls": 3,
        "max_input_tokens": 100,
        "max_output_tokens": 100,
        "max_cached_tokens": 50,
        "max_cache_write_tokens": 50,
        "max_tool_calls": 3,
        "max_tool_output_bytes": 1000,
        "max_artifact_bytes": 1000,
        "max_patch_bytes": 1000,
        "max_retries": 2,
        "max_wall_time_ms": 1000,
        "provider_deadline_ms": 500,
        "max_known_cost_usd": 5.0,
        "max_estimated_cost_usd": 6.0,
    }
    payload.update(updates)
    return TaskCeilings.model_validate(payload)


def _usage(
    *,
    usage_status: UsageStatus = UsageStatus.KNOWN,
    cost_status: CostStatus = CostStatus.KNOWN,
    input_tokens: int = 4,
    output_tokens: int = 3,
    cached_tokens: int = 1,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.25,
) -> ProviderUsageReport:
    return ProviderUsageReport(
        usage_status=usage_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_status=cost_status,
        cost_usd=cost_usd,
        response_id="response.offline",
    )


class ImmediateProvider:
    def __init__(self, usage: ProviderUsageReport | None = None) -> None:
        self.usage = usage or _usage()
        self.calls: list[tuple[ProviderCallControl, CredentialReference | None]] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        self.calls.append((control, credential_reference))
        return ProviderInvocation(response={"request": request}, usage=self.usage)


class FailingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        self.calls += 1
        raise self.error


class HungProvider:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.observed_cancellation = threading.Event()
        self.timeout_ms = 0
        self.cancelled_reservations: list[str] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        self.timeout_ms = control.timeout_ms
        self.started.set()
        if control.cancellation_event.wait(timeout=1.0):
            self.observed_cancellation.set()
        return ProviderInvocation(response="cancelled", usage=_usage())

    def cancel(self, reservation_id: str) -> None:
        self.cancelled_reservations.append(reservation_id)


def test_aggregate_usage_reconciles_and_exhaustion_prevents_next_action() -> None:
    ledger = AggregateBudgetLedger(
        _ceilings(max_model_calls=1, max_tool_calls=1, max_tool_output_bytes=8)
    )
    provider = ImmediateProvider()
    controller = CompositeProviderController(ledger)

    result = controller.call(
        provider,
        {"prompt": "offline"},
        input_tokens=4,
        max_output_tokens=10,
        max_cached_tokens=2,
        estimated_cost_usd=0.4,
    )
    tool_reservation = ledger.reserve_tool_call(max_output_bytes=8)
    snapshot = ledger.complete_tool_call(tool_reservation, output_bytes=7, latency_ms=2.0)

    assert result.status is ProviderCallStatus.SUCCEEDED
    assert snapshot.model_calls == 1
    assert snapshot.input_tokens == 4
    assert snapshot.output_tokens == 3
    assert snapshot.cached_tokens == 1
    assert snapshot.cache_write_tokens == 0
    assert snapshot.known_cost_usd == pytest.approx(0.25)
    assert snapshot.estimated_cost_usd == 0.0
    assert snapshot.unknown_cost_events == 0
    assert snapshot.tool_calls == 1
    assert snapshot.tool_output_bytes == 7
    assert snapshot.active_reservations == 0
    assert snapshot.reconciled is True

    rejected = controller.call(
        provider,
        {"prompt": "must not dispatch"},
        input_tokens=1,
        max_output_tokens=1,
        estimated_cost_usd=0.1,
    )

    assert rejected.status is ProviderCallStatus.REJECTED
    assert rejected.failure.kind is ProviderFailureKind.BUDGET_EXHAUSTED
    assert rejected.failure.budget_metric == "model_calls"
    assert len(provider.calls) == 1


def test_provider_reservation_allows_independent_cache_read_write_caps() -> None:
    ledger = AggregateBudgetLedger(
        _ceilings(
            max_input_tokens=100,
            max_cached_tokens=90,
            max_cache_write_tokens=90,
        )
    )

    reservation = ledger.reserve_provider_call(
        input_tokens=100,
        max_output_tokens=10,
        max_cached_tokens=90,
        max_cache_write_tokens=90,
        estimated_cost_usd=0.25,
    )

    assert reservation.cached_tokens == 90
    assert reservation.cache_write_tokens == 90


def test_provider_usage_rejects_cache_subcategories_above_input_tokens() -> None:
    with pytest.raises(ValidationError, match="cache-write tokens"):
        ProviderUsageReport(
            usage_status=UsageStatus.KNOWN,
            input_tokens=4,
            output_tokens=1,
            cached_tokens=3,
            cache_write_tokens=2,
            cost_status=CostStatus.KNOWN,
            cost_usd=0.1,
        )


def test_cache_write_tokens_are_reserved_accounted_and_capped() -> None:
    ledger = AggregateBudgetLedger(
        _ceilings(max_model_calls=2, max_cached_tokens=4, max_cache_write_tokens=2)
    )
    provider = ImmediateProvider(_usage(cached_tokens=1, cache_write_tokens=2))
    controller = CompositeProviderController(ledger)

    result = controller.call(
        provider,
        {"prompt": "cache-write"},
        input_tokens=4,
        max_output_tokens=3,
        max_cached_tokens=1,
        max_cache_write_tokens=2,
        estimated_cost_usd=0.4,
    )

    assert result.status is ProviderCallStatus.SUCCEEDED
    assert result.ledger.cached_tokens == 1
    assert result.ledger.cache_write_tokens == 2

    rejected = controller.call(
        provider,
        {"prompt": "cache-write-cap"},
        input_tokens=1,
        max_output_tokens=1,
        max_cache_write_tokens=1,
        estimated_cost_usd=0.1,
    )

    assert rejected.status is ProviderCallStatus.REJECTED
    assert rejected.failure.kind is ProviderFailureKind.BUDGET_EXHAUSTED
    assert rejected.failure.budget_metric == "cache_write_tokens"


def test_cache_read_and_write_reservations_allow_independent_worst_cases() -> None:
    ledger = AggregateBudgetLedger(_ceilings())

    reservation = ledger.reserve_provider_call(
        input_tokens=2,
        max_output_tokens=1,
        max_cached_tokens=2,
        max_cache_write_tokens=2,
        estimated_cost_usd=0.1,
    )

    assert reservation.cached_tokens == 2
    assert reservation.cache_write_tokens == 2
    assert ledger.cancel_reservation(reservation).active_reservations == 0


def test_preflight_reservations_are_atomic_across_competing_calls() -> None:
    ledger = AggregateBudgetLedger(_ceilings(max_model_calls=1))
    barrier = threading.Barrier(3)
    reservations = []
    failures = []

    def reserve() -> None:
        barrier.wait()
        try:
            reservations.append(
                ledger.reserve_provider_call(
                    input_tokens=10,
                    max_output_tokens=10,
                    estimated_cost_usd=1.0,
                )
            )
        except BudgetExhaustedError as exc:
            failures.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=1.0)

    assert len(reservations) == 1
    assert len(failures) == 1
    assert failures[0].metric == "model_calls"
    assert ledger.snapshot().active_reservations == 1
    ledger.cancel_reservation(reservations[0])


@pytest.mark.parametrize(
    ("cost_status", "expected_known", "expected_estimated"),
    [
        (CostStatus.KNOWN, 0.25, 0.0),
        (CostStatus.ESTIMATED, 0.0, 0.25),
    ],
)
def test_shaped_post_send_failures_preserve_known_and_estimated_cost(
    cost_status: CostStatus,
    expected_known: float,
    expected_estimated: float,
) -> None:
    usage_status = UsageStatus.KNOWN if cost_status is CostStatus.KNOWN else UsageStatus.ESTIMATED
    usage = _usage(usage_status=usage_status, cost_status=cost_status)
    provider = FailingProvider(
        ProviderInvocationError(request_sent=True, usage=usage)
    )
    controller = CompositeProviderController(AggregateBudgetLedger(_ceilings()))

    result = controller.call(
        provider,
        "offline",
        input_tokens=4,
        max_output_tokens=10,
        max_cached_tokens=2,
        estimated_cost_usd=0.4,
        expected_cost_status=cost_status,
    )

    assert result.status is ProviderCallStatus.FAILED
    assert result.failure.kind is ProviderFailureKind.POST_SEND_FAILURE
    assert result.failure.cost_status is cost_status
    assert result.failure.ambiguous_post_send is False
    assert result.failure.accounting_healthy is True
    assert result.ledger.known_cost_usd == pytest.approx(expected_known)
    assert result.ledger.estimated_cost_usd == pytest.approx(expected_estimated)
    assert result.ledger.unknown_cost_events == 0


def test_ambiguous_post_send_failure_is_unknown_and_closes_provider_budget() -> None:
    provider = FailingProvider(RuntimeError("must-not-persist-sk-live-secret"))
    ledger = AggregateBudgetLedger(_ceilings())
    controller = CompositeProviderController(ledger)

    result = controller.call(
        provider,
        "offline",
        input_tokens=4,
        max_output_tokens=10,
        estimated_cost_usd=0.4,
    )

    assert result.status is ProviderCallStatus.FAILED
    assert result.failure.kind is ProviderFailureKind.POST_SEND_FAILURE
    assert result.failure.usage_status is UsageStatus.UNKNOWN
    assert result.failure.cost_status is CostStatus.UNKNOWN
    assert result.failure.ambiguous_post_send is True
    assert result.failure.accounting_healthy is False
    assert result.ledger.known_cost_usd == 0.0
    assert result.ledger.estimated_cost_usd == 0.0
    assert result.ledger.unknown_cost_events == 1
    assert result.ledger.unknown_usage_events == 1
    assert result.ledger.reconciled is False
    assert result.ledger.promotion_eligible is False
    assert "must-not-persist" not in result.model_dump_json()

    next_result = controller.call(
        provider,
        "blocked",
        input_tokens=1,
        max_output_tokens=1,
        estimated_cost_usd=0.1,
    )
    assert next_result.status is ProviderCallStatus.REJECTED
    assert next_result.failure.budget_metric == "provider_accounting_unknown"
    assert provider.calls == 1


def test_hung_provider_is_cancelled_at_remaining_request_deadline() -> None:
    ledger = AggregateBudgetLedger(
        _ceilings(max_wall_time_ms=250, provider_deadline_ms=40)
    )
    provider = HungProvider()
    controller = CompositeProviderController(ledger)
    started = time.monotonic()

    result = controller.call(
        provider,
        "hang",
        input_tokens=1,
        max_output_tokens=1,
        estimated_cost_usd=0.1,
    )
    elapsed = time.monotonic() - started

    assert result.status is ProviderCallStatus.DEADLINE_EXCEEDED
    assert result.timeout_ms <= 40
    assert provider.timeout_ms == result.timeout_ms
    assert elapsed < 0.5
    assert provider.observed_cancellation.wait(timeout=0.2)
    assert provider.cancelled_reservations == [result.reservation_id]
    assert result.failure.request_sent is True
    assert result.failure.cost_status is CostStatus.UNKNOWN
    assert result.failure.ambiguous_post_send is True
    assert result.ledger.promotion_eligible is False


def test_provider_timeout_is_limited_by_remaining_wall_time() -> None:
    now = [0.0]
    ledger = AggregateBudgetLedger(
        _ceilings(max_wall_time_ms=100, provider_deadline_ms=90),
        clock=lambda: now[0],
    )
    now[0] = 0.075
    provider = ImmediateProvider()

    result = CompositeProviderController(ledger).call(
        provider,
        "offline",
        input_tokens=4,
        max_output_tokens=3,
        max_cached_tokens=1,
        estimated_cost_usd=0.3,
    )

    assert result.status is ProviderCallStatus.SUCCEEDED
    assert 20 <= result.timeout_ms <= 25
    assert provider.calls[0][0].timeout_ms == result.timeout_ms


def test_external_cancellation_signals_provider_and_records_unknown_sent_usage() -> None:
    ledger = AggregateBudgetLedger(_ceilings(provider_deadline_ms=500))
    provider = HungProvider()
    cancellation = threading.Event()
    results = []

    def call() -> None:
        results.append(
            CompositeProviderController(ledger).call(
                provider,
                "cancel",
                input_tokens=1,
                max_output_tokens=1,
                estimated_cost_usd=0.1,
                cancellation_event=cancellation,
            )
        )

    worker = threading.Thread(target=call)
    worker.start()
    assert provider.started.wait(timeout=0.5)
    cancellation.set()
    worker.join(timeout=0.5)

    assert not worker.is_alive()
    assert results[0].status is ProviderCallStatus.CANCELLED
    assert provider.observed_cancellation.wait(timeout=0.2)
    assert results[0].failure.ambiguous_post_send is True
    assert results[0].ledger.unknown_cost_events == 1


def test_credential_transport_serializes_references_but_never_secret_values(
    monkeypatch,
) -> None:
    secret = "sk-live-never-serialize-this"
    monkeypatch.setenv("AGINTOR_TEST_API_KEY", secret)
    reference = CredentialReference(
        provider_name="OpenAI",
        api_key_env="AGINTOR_TEST_API_KEY",
        api_key_file_env="AGINTOR_TEST_KEY_FILE",
    )
    serialized = reference.model_dump_json()

    assert json.loads(serialized) == {
        "provider_name": "openai",
        "api_key_env": "AGINTOR_TEST_API_KEY",
        "api_key_file_env": "AGINTOR_TEST_KEY_FILE",
    }
    assert secret not in serialized
    assert os.environ[reference.api_key_env] == secret
    with pytest.raises(ValidationError, match="api_key"):
        CredentialReference.model_validate(
            {
                "provider_name": "openai",
                "api_key_env": "AGINTOR_TEST_API_KEY",
                "api_key": secret,
            }
        )

    provider = ImmediateProvider()
    result = CompositeProviderController(AggregateBudgetLedger(_ceilings())).call(
        provider,
        "offline",
        input_tokens=4,
        max_output_tokens=4,
        max_cached_tokens=1,
        estimated_cost_usd=0.4,
        credential_reference=reference,
    )

    assert result.status is ProviderCallStatus.SUCCEEDED
    assert provider.calls[0][1] == reference
    assert secret not in result.model_dump_json()


def test_retry_and_output_reservation_overrun_are_typed_in_aggregate_snapshot() -> None:
    ledger = AggregateBudgetLedger(_ceilings(max_retries=1))
    provider = ImmediateProvider(_usage(output_tokens=5))
    result = CompositeProviderController(ledger).call(
        provider,
        "retry",
        input_tokens=4,
        max_output_tokens=2,
        max_cached_tokens=1,
        estimated_cost_usd=0.4,
        is_retry=True,
    )

    assert result.status is ProviderCallStatus.FAILED
    assert result.failure.kind is ProviderFailureKind.ACCOUNTING_INVALID
    assert result.ledger.retries == 1
    assert "output_tokens_reservation_exceeded" in result.ledger.violations
    assert result.ledger.promotion_eligible is False

    blocked = CompositeProviderController(ledger).call(
        provider,
        "blocked-after-accounting-violation",
        input_tokens=1,
        max_output_tokens=1,
        estimated_cost_usd=0.1,
    )
    assert blocked.status is ProviderCallStatus.REJECTED
    assert blocked.failure.budget_metric == "ledger_accounting_invalid"


def test_known_cost_reservations_cannot_collectively_overspend() -> None:
    ledger = AggregateBudgetLedger(
        _ceilings(max_known_cost_usd=0.3, max_estimated_cost_usd=1.0)
    )
    first = ledger.reserve_provider_call(
        input_tokens=1,
        max_output_tokens=1,
        estimated_cost_usd=0.2,
    )

    with pytest.raises(BudgetExhaustedError) as raised:
        ledger.reserve_provider_call(
            input_tokens=1,
            max_output_tokens=1,
            estimated_cost_usd=0.2,
        )

    assert raised.value.metric == "known_cost_usd"
    ledger.cancel_reservation(first)


def test_actual_cost_above_reservation_is_an_accounting_violation() -> None:
    provider = ImmediateProvider(_usage(cost_usd=0.25))
    ledger = AggregateBudgetLedger(_ceilings())

    result = CompositeProviderController(ledger).call(
        provider,
        "under-reserved",
        input_tokens=4,
        max_output_tokens=3,
        max_cached_tokens=1,
        estimated_cost_usd=0.1,
    )

    assert result.status is ProviderCallStatus.FAILED
    assert "cost_reservation_exceeded" in result.ledger.violations
    assert result.ledger.promotion_eligible is False
