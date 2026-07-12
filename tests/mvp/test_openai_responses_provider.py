from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agintor.runtime.kernel.openai_responses_provider as openai_provider_module
from agintor.core.identity import canonical_identity_digest
from agintor.contracts.epochs import TaskCeilings
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessPromptCachePolicy,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)
from agintor.runtime.kernel.composite_budget import (
    AggregateBudgetLedger,
    CostStatus,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import (
    CompositeProviderController,
    CredentialReference,
    ProviderCallControl,
    ProviderCallStatus,
    ProviderFailureKind,
    ProviderExecutionProvenance,
    ProviderInvocationError,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallRequest,
    ActorTerminalTurn,
    ActorToolRequest,
    CompositeRuntime,
    CompositeRuntimeError,
    PreCallContextManifest,
)
from agintor.runtime.kernel.openai_responses_provider import OpenAIResponsesProvider
from agintor.runtime.sdk.harness_entrypoint import default_harness_adapter_registry


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-openai-responses-provider")


def _command_policy() -> HarnessCommandContainerPolicy:
    return HarnessCommandContainerPolicy(
        image="python@sha256:" + "e" * 64,
        timeout_s=30.0,
        memory_bytes=256 * 1024 * 1024,
        cpu_count=1.0,
        pids_limit=128,
        output_bytes=1_000_000,
        tmpfs_bytes=64 * 1024 * 1024,
        nofile_limit=256,
    )


def _profile(
    *,
    model: str = "gpt-5.6-terra",
    api_key_env: str | None = "OPENAI_API_KEY",
    api_key_file_env: str | None = None,
    base_url: str = "https://api.openai.com/v1",
    base_url_env: str | None = None,
    prompt_cache: bool = True,
    input_rate: float = 2.5,
    cached_input_rate: float = 0.25,
    cache_write_rate: float = 3.125,
    output_rate: float = 15.0,
) -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id=f"openai.{model.replace('.', '-').replace('/', '-')}.test",
        provider="openai",
        model=model,
        endpoint=HarnessProviderEndpoint(
            base_url=None if base_url_env else base_url,
            base_url_env=base_url_env,
            api_key_env=api_key_env,
            api_key_file_env=api_key_file_env,
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.2,
            top_p=0.9,
            max_output_tokens=4096,
            reasoning_effort="none",
            service_tier="default",
            store=False,
            parallel_tool_calls=False,
            text_verbosity="low",
        ),
        prompt_cache_policy=(
            HarnessPromptCachePolicy(
                mode="explicit",
                prompt_cache_key="agintor-openai-test-cache",
                ttl="30m",
                breakpoint="static_prefix",
            )
            if prompt_cache
            else HarnessPromptCachePolicy()
        ),
        price_schedule=HarnessUsdPriceSchedule(
            billing_mode="paid",
            input_usd_per_million_tokens=input_rate,
            output_usd_per_million_tokens=output_rate,
            cached_input_usd_per_million_tokens=cached_input_rate,
            cache_write_usd_per_million_tokens=cache_write_rate,
        ),
        command_container_policy=_command_policy(),
    )


def _request(
    *,
    allowed_tool_ids: tuple[str, ...] = ("repo.read",),
    max_output_tokens: int = 2048,
) -> ActorCallRequest:
    context = PreCallContextManifest(
        call_id="call.openai",
        actor_id="actor.writer",
        task_envelope_digest=_digest("task"),
        reads=(),
    )
    return ActorCallRequest(
        run_id="run.openai",
        compiled_semantic_digest=_digest("compiled"),
        call_id="call.openai",
        actor_id="actor.writer",
        call_kind="initial",
        instruction="Repair the repository.",
        allowed_tool_ids=allowed_tool_ids,
        budget_share_bps=10_000,
        context=context,
        input_token_estimate=1200,
        max_output_tokens=max_output_tokens,
    )


def _request_with_instruction(
    instruction: str,
    *,
    input_token_estimate: int = 0,
) -> ActorCallRequest:
    payload = _request(max_output_tokens=128).model_dump(
        mode="python",
        exclude={"request_digest"},
    )
    payload["instruction"] = instruction
    payload["input_token_estimate"] = input_token_estimate
    return ActorCallRequest.model_validate(payload)


def _credential_reference(profile: HarnessDeploymentProfile) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=profile.endpoint.api_key_env,
        api_key_file_env=profile.endpoint.api_key_file_env,
    )


def _ceilings(**updates: Any) -> TaskCeilings:
    payload = {
        "max_model_calls": 3,
        "max_input_tokens": 500_000,
        "max_output_tokens": 20_000,
        "max_cached_tokens": 20_000,
        "max_cache_write_tokens": 20_000,
        "max_tool_calls": 3,
        "max_tool_output_bytes": 1000,
        "max_artifact_bytes": 1000,
        "max_patch_bytes": 1000,
        "max_retries": 0,
        "max_wall_time_ms": 1000,
        "provider_deadline_ms": 500,
        "max_known_cost_usd": 5.0,
        "max_estimated_cost_usd": 6.0,
    }
    payload.update(updates)
    return TaskCeilings.model_validate(payload)


def _control(*, cancelled: bool = False) -> ProviderCallControl:
    event = threading.Event()
    if cancelled:
        event.set()
    return ProviderCallControl(
        reservation_id="budget.provider.000001",
        timeout_ms=5_000,
        deadline_monotonic=time.monotonic() + 5,
        cancellation_event=event,
    )


_DEFAULT_USAGE = object()


def _terminal_response(
    *,
    output_text: str = "done",
    usage: dict[str, Any] | None | object = _DEFAULT_USAGE,
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "id": "resp_test_1",
        "status": status,
        "output_text": json.dumps(
            {
                "turn_kind": "terminal",
                "output": {
                    "output_text": output_text,
                    "artifact_payload_entries": [],
                    "final_patch": None,
                },
            }
        ),
        "usage": (
            {
                "input_tokens": 1000,
                "output_tokens": 200,
                "input_tokens_details": {
                    "cached_tokens": 300,
                    "cache_write_tokens": 100,
                },
            }
            if usage is _DEFAULT_USAGE
            else usage
        ),
    }


class _FakeResponses:
    def __init__(self, owner: "_FakeClient", response: Any = None, error: Exception | None = None) -> None:
        self._owner = owner
        self._response = response
        self._error = error

    def create(self, **payload: Any) -> Any:
        self._owner.payloads.append(payload)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.responses = _FakeResponses(self, response=response, error=error)


class _FakeClientFactory:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.client = _FakeClient(response=response, error=error)

    def __call__(self, **kwargs: Any) -> _FakeClient:
        self.calls.append(kwargs)
        return self.client


def test_openai_responses_provider_builds_frozen_payload_and_accounts_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(
        profile,
        deployment=profile.to_deployment_identity(),
        client_factory=factory,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    invocation = provider.invoke(
        _request(),
        control=_control(),
        credential_reference=_credential_reference(profile),
    )

    assert isinstance(invocation.response, ActorTerminalTurn)
    assert invocation.response.output.output_text == "done"
    assert invocation.usage.usage_status is UsageStatus.KNOWN
    assert invocation.usage.cost_status is CostStatus.KNOWN
    assert invocation.usage.input_tokens == 1000
    assert invocation.usage.output_tokens == 200
    assert invocation.usage.cached_tokens == 300
    assert invocation.usage.cache_write_tokens == 100
    assert invocation.usage.cost_usd == pytest.approx(0.0048875)
    assert invocation.usage.response_id == "resp_test_1"

    assert factory.calls == [
        {
            "api_key": "sk-test-not-real",
            "base_url": "https://api.openai.com/v1",
            "timeout": pytest.approx(5.0, rel=0.05),
            "max_retries": 0,
        }
    ]
    payload = factory.client.payloads[0]
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["service_tier"] == "default"
    assert payload["store"] is False
    assert payload["parallel_tool_calls"] is False
    assert payload["text"]["verbosity"] == "low"
    assert payload["text"]["format"]["name"] == "agintor_terminal_turn"
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["type"] == "object"
    assert "oneOf" not in payload["text"]["format"]["schema"]
    assert payload["prompt_cache_key"] == "agintor-openai-test-cache"
    assert payload["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    static_prefix = payload["input"][0]["content"][0]
    assert static_prefix["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert static_prefix["text"] != payload["instructions"]
    assert len((payload["instructions"] + " " + static_prefix["text"]).split()) >= 1024
    assert payload["tools"][0]["name"] == "repo_read"
    assert payload["tools"][0]["parameters"]["required"] == ["arguments_json"]
    assert payload["tools"][0]["parameters"]["additionalProperties"] is False
    assert "sk-test-not-real" not in json.dumps(payload)
    assert provider.execution_provenance == ProviderExecutionProvenance(
        execution_mode="live_provider",
        live_inference_status="completed",
        real_inference_requests_sent=1,
    )


def test_openai_responses_provider_normalizes_canonical_base_url_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(base_url="https://api.openai.com/v1/")
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    provider.invoke(
        _request(),
        control=_control(),
        credential_reference=_credential_reference(profile),
    )

    assert factory.calls[0]["base_url"] == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    "untrusted_base_url",
    (
        "http://api.openai.com/v1",
        "https://user:password@api.openai.com/v1",
        "https://api.openai.com:443/v1",
        "https://api.openai.com.evil.example/v1",
        "https://api.openai.com/v1?destination=elsewhere",
        "https://api.openai.com/v1#fragment",
        "https://api.openai.com/v1/extra",
        "https://api.openai.com%2Fevil.example/v1",
        "https://аpi.openai.com/v1",
    ),
)
def test_openai_responses_provider_rejects_untrusted_destination_before_credential_resolution(
    monkeypatch: pytest.MonkeyPatch,
    untrusted_base_url: str,
) -> None:
    profile = _profile(
        api_key_env=None,
        api_key_file_env="OPENAI_API_KEY_FILE",
        base_url_env="OPENAI_BASE_URL",
    )
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    credential_reads: list[str] = []

    monkeypatch.setenv("OPENAI_BASE_URL", untrusted_base_url)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", "must-not-be-read")

    def read_api_key_file(raw_path: str) -> str:
        credential_reads.append(raw_path)
        return "sk-test-not-real"

    monkeypatch.setattr(
        openai_provider_module,
        "_read_api_key_file",
        read_api_key_file,
    )

    with pytest.raises(ProviderInvocationError) as raised:
        provider.invoke(
            _request(),
            control=_control(),
            credential_reference=_credential_reference(profile),
        )

    assert raised.value.request_sent is False
    assert credential_reads == []
    assert factory.calls == []
    assert provider.execution_provenance.real_inference_requests_sent == 0


def test_openai_responses_provider_uses_frozen_luna_identity_and_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(
        model="gpt-5.6-luna",
        input_rate=1.0,
        cached_input_rate=0.10,
        cache_write_rate=1.25,
        output_rate=6.0,
        prompt_cache=False,
    )
    factory = _FakeClientFactory(
        response=_terminal_response(
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {
                    "cached_tokens": 2,
                    "cache_write_tokens": 1,
                },
            }
        )
    )
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    invocation = provider.invoke(
        _request(),
        control=_control(),
        credential_reference=_credential_reference(profile),
    )

    assert factory.client.payloads[0]["model"] == "gpt-5.6-luna"
    assert invocation.usage.cost_usd == pytest.approx(0.00003845)


def test_openai_responses_provider_reserves_payload_overhead_and_cache_caps() -> None:
    profile = _profile()
    provider = OpenAIResponsesProvider(profile, client_factory=_FakeClientFactory())
    request = _request(max_output_tokens=8192)

    reservation = provider.provider_request_reservation(request)

    assert reservation.input_tokens > request.input_token_estimate
    assert reservation.max_output_tokens == profile.decoding_policy.max_output_tokens
    assert reservation.max_cached_tokens > 1024
    assert reservation.max_cache_write_tokens == reservation.max_cached_tokens
    assert reservation.max_cached_tokens <= reservation.input_tokens
    expected_max_cost = (
        reservation.max_cache_write_tokens
        * profile.price_schedule.cache_write_usd_per_million_tokens
        + (reservation.input_tokens - reservation.max_cache_write_tokens)
        * profile.price_schedule.input_usd_per_million_tokens
        + reservation.max_output_tokens
        * profile.price_schedule.output_usd_per_million_tokens
    ) / 1_000_000
    assert reservation.max_known_cost_usd == pytest.approx(expected_max_cost)


@pytest.mark.parametrize(
    "dense_instruction",
    (
        "界🙂e\u0301\u200d" * 4_000,
        "{}[](),:;=<>+-*/%&|^~!\\'\"`_\n" * 1_000,
    ),
    ids=("utf8-dense-unicode", "punctuation-dense-code"),
)
def test_openai_reservation_upper_bounds_complete_utf8_request_payload(
    dense_instruction: str,
) -> None:
    provider = OpenAIResponsesProvider(
        _profile(),
        client_factory=_FakeClientFactory(),
    )
    request = _request_with_instruction(dense_instruction)
    payload = provider._response_payload(request)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_utf8_bytes = len(serialized.encode("utf-8"))
    cache_prefix_utf8_bytes = len(
        json.dumps(
            openai_provider_module._cache_prefix_payload(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    reservation = provider.provider_request_reservation(request)

    assert reservation.input_tokens >= payload_utf8_bytes
    assert reservation.input_tokens >= request.input_token_estimate
    assert reservation.max_cached_tokens == reservation.max_cache_write_tokens
    assert reservation.max_cached_tokens >= cache_prefix_utf8_bytes
    assert 0 < reservation.max_cached_tokens < reservation.input_tokens
    # These payloads are adversarial to the former character-count / 4
    # heuristic, which cannot be a byte-level upper bound.
    assert (len(serialized) + 3) // 4 + 512 < payload_utf8_bytes


def test_openai_responses_provider_max_cost_respects_distinct_cache_caps() -> None:
    profile = _profile(
        input_rate=1.0,
        cached_input_rate=5.0,
        cache_write_rate=3.0,
        output_rate=2.0,
    )
    provider = OpenAIResponsesProvider(profile, client_factory=_FakeClientFactory())

    reservation = provider.provider_request_reservation(_request())

    cached_tokens = min(reservation.max_cached_tokens, reservation.input_tokens)
    cache_write_tokens = min(
        reservation.max_cache_write_tokens,
        reservation.input_tokens - cached_tokens,
    )
    uncached_tokens = reservation.input_tokens - cached_tokens - cache_write_tokens
    expected_max_cost = (
        cached_tokens * profile.price_schedule.cached_input_usd_per_million_tokens
        + cache_write_tokens * profile.price_schedule.cache_write_usd_per_million_tokens
        + uncached_tokens * profile.price_schedule.input_usd_per_million_tokens
        + reservation.max_output_tokens
        * profile.price_schedule.output_usd_per_million_tokens
    ) / 1_000_000
    assert reservation.max_known_cost_usd == pytest.approx(expected_max_cost)


def test_openai_responses_provider_fails_closed_for_short_context_pricing_guard() -> None:
    profile = _profile(model="gpt-5.6-luna")
    provider = OpenAIResponsesProvider(profile, client_factory=_FakeClientFactory())
    request = _request(max_output_tokens=128).model_copy(
        update={"input_token_estimate": 249_900}
    )

    with pytest.raises(ValueError, match="short-context GPT-5.6 pricing"):
        provider.provider_request_reservation(request)


def test_openai_dense_unicode_long_context_guard_fails_before_provider_send() -> None:
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(
        _profile(model="gpt-5.6-terra"),
        client_factory=factory,
    )
    request = _request_with_instruction("🙂" * 65_000)
    payload_bytes = len(
        json.dumps(
            provider._response_payload(request),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert payload_bytes > 250_000
    runtime = CompositeRuntime.__new__(CompositeRuntime)
    runtime.provider = provider

    with pytest.raises(CompositeRuntimeError) as raised:
        CompositeRuntime._provider_request_reservation(runtime, request)

    assert raised.value.kind == "provider_reservation_failed"
    assert isinstance(raised.value.__cause__, ValueError)
    assert "short-context GPT-5.6 pricing" in str(raised.value.__cause__)
    assert factory.calls == []
    assert provider.execution_provenance.real_inference_requests_sent == 0


def test_controller_rejects_openai_reservation_before_provider_send_when_ceilings_are_too_low() -> None:
    profile = _profile()
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    request = _request()
    reservation = provider.provider_request_reservation(request)
    ledger = AggregateBudgetLedger(
        _ceilings(
            max_input_tokens=reservation.input_tokens - 1,
            max_cached_tokens=max(reservation.max_cached_tokens, 1),
            max_cache_write_tokens=max(reservation.max_cache_write_tokens, 1),
        )
    )

    result = CompositeProviderController(ledger).call(
        provider,
        request,
        input_tokens=reservation.input_tokens,
        max_output_tokens=reservation.max_output_tokens,
        max_cached_tokens=reservation.max_cached_tokens,
        max_cache_write_tokens=reservation.max_cache_write_tokens,
        estimated_cost_usd=1.0,
        credential_reference=_credential_reference(profile),
    )

    assert result.status is ProviderCallStatus.REJECTED
    assert result.failure is not None
    assert result.failure.kind is ProviderFailureKind.BUDGET_EXHAUSTED
    assert factory.calls == []
    assert provider.execution_provenance.real_inference_requests_sent == 0


def test_runtime_rejects_paid_openai_call_before_send_when_known_cost_cap_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    request = _request()
    reservation = provider.provider_request_reservation(request)
    assert reservation.max_known_cost_usd is not None
    assert reservation.max_known_cost_usd > 0.0
    ceilings = _ceilings(
        max_model_calls=1,
        max_input_tokens=reservation.input_tokens,
        max_output_tokens=reservation.max_output_tokens,
        max_cached_tokens=reservation.max_cached_tokens,
        max_cache_write_tokens=reservation.max_cache_write_tokens,
        max_known_cost_usd=0.0,
        max_estimated_cost_usd=1.0,
    )
    ledger = AggregateBudgetLedger(ceilings)
    call = SimpleNamespace(
        call_id=request.call_id,
        actor_id=request.actor_id,
        budget_share_bps=10_000,
    )
    runtime = CompositeRuntime.__new__(CompositeRuntime)
    runtime.provider = provider
    runtime.provider_controller = CompositeProviderController(ledger)
    runtime.credential_reference = _credential_reference(profile)
    runtime.task = SimpleNamespace(ceilings=ceilings)
    runtime.plan = SimpleNamespace(actor_calls=(call,))
    runtime._context_for = lambda received: request.context
    runtime._request_for = lambda received, context, **kwargs: request
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    with pytest.raises(CompositeRuntimeError) as raised:
        CompositeRuntime._execute_call(runtime, call, "stage.openai")

    assert raised.value.kind == "provider_call_failed"
    assert raised.value.provider_result is not None
    assert raised.value.provider_result.status is ProviderCallStatus.REJECTED
    assert raised.value.provider_result.failure is not None
    assert raised.value.provider_result.failure.budget_metric == "known_cost_usd"
    assert factory.calls == []
    assert provider.execution_provenance.real_inference_requests_sent == 0


def test_openai_worst_case_cost_reservation_reconciles_to_actual_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    request = _request()
    reservation = provider.provider_request_reservation(request)
    assert reservation.max_known_cost_usd is not None

    class RecordingLedger(AggregateBudgetLedger):
        reserved_cost_usd: float | None = None

        def reserve_provider_call(self, **kwargs: Any):
            self.reserved_cost_usd = kwargs["estimated_cost_usd"]
            return super().reserve_provider_call(**kwargs)

    ledger = RecordingLedger(
        _ceilings(
            max_model_calls=1,
            max_input_tokens=reservation.input_tokens,
            max_output_tokens=reservation.max_output_tokens,
            max_cached_tokens=reservation.max_cached_tokens,
            max_cache_write_tokens=reservation.max_cache_write_tokens,
            max_known_cost_usd=reservation.max_known_cost_usd,
            max_estimated_cost_usd=reservation.max_known_cost_usd,
        )
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    result = CompositeProviderController(ledger).call(
        provider,
        request,
        input_tokens=reservation.input_tokens,
        max_output_tokens=reservation.max_output_tokens,
        max_cached_tokens=reservation.max_cached_tokens,
        max_cache_write_tokens=reservation.max_cache_write_tokens,
        estimated_cost_usd=reservation.max_known_cost_usd,
        credential_reference=_credential_reference(profile),
    )

    assert result.status is ProviderCallStatus.SUCCEEDED
    assert ledger.reserved_cost_usd == pytest.approx(reservation.max_known_cost_usd)
    assert result.invocation is not None
    assert result.ledger.known_cost_usd == pytest.approx(result.invocation.usage.cost_usd)
    assert result.ledger.known_cost_usd < reservation.max_known_cost_usd
    assert result.ledger.active_reservations == 0
    assert result.ledger.reconciled is True
    assert result.ledger.healthy is True


def test_composite_runtime_uses_provider_request_reservation_hook() -> None:
    request = _request()

    class ReservationProvider:
        def provider_request_reservation(self, received: ActorCallRequest) -> dict[str, int]:
            assert received == request
            return {
                "input_tokens": 123,
                "max_output_tokens": 45,
                "max_cached_tokens": 67,
                "max_cache_write_tokens": 89,
            }

    runtime = CompositeRuntime.__new__(CompositeRuntime)
    runtime.provider = ReservationProvider()

    reservation = CompositeRuntime._provider_request_reservation(runtime, request)

    assert reservation.input_tokens == 123
    assert reservation.max_output_tokens == 45
    assert reservation.max_cached_tokens == 67
    assert reservation.max_cache_write_tokens == 89
    assert reservation.max_known_cost_usd is None


def test_composite_runtime_reservation_hook_failure_is_pre_send_runtime_error() -> None:
    class BadReservationProvider:
        def provider_request_reservation(self, request: ActorCallRequest) -> dict[str, int]:
            raise ValueError("cannot reserve")

    runtime = CompositeRuntime.__new__(CompositeRuntime)
    runtime.provider = BadReservationProvider()

    with pytest.raises(CompositeRuntimeError) as raised:
        CompositeRuntime._provider_request_reservation(runtime, _request())

    assert raised.value.kind == "provider_reservation_failed"


def test_openai_responses_provider_maps_tool_request_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(prompt_cache=False)
    response = {
        "id": "resp_tool",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "repo_read",
                "call_id": "tool.1",
                "arguments": json.dumps(
                    {
                        "arguments_json": json.dumps(
                            {"path": "README.md"},
                            sort_keys=True,
                        )
                    },
                    sort_keys=True,
                ),
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {},
        },
    }
    factory = _FakeClientFactory(response=response)
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    invocation = provider.invoke(
        _request(),
        control=_control(),
        credential_reference=_credential_reference(profile),
    )

    assert isinstance(invocation.response, ActorToolRequest)
    assert invocation.response.tool_id == "repo.read"
    assert invocation.response.arguments == {"path": "README.md"}
    assert "prompt_cache_key" not in factory.client.payloads[0]
    assert factory.client.payloads[0]["prompt_cache_options"] == {
        "mode": "explicit",
        "ttl": "30m",
    }


def test_openai_responses_provider_reads_key_file_reference_without_serializing_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = _profile(api_key_env=None, api_key_file_env="OPENAI_API_KEY_FILE")
    key_file = tmp_path / "openai.key"
    key_file.write_text("sk-file-not-real\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key_file))
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)

    provider.invoke(
        _request(),
        control=_control(),
        credential_reference=_credential_reference(profile),
    )

    assert factory.calls[0]["api_key"] == "sk-file-not-real"
    assert "sk-file-not-real" not in json.dumps(factory.client.payloads[0])


def test_openai_responses_provider_rejects_missing_or_ambiguous_credentials_pre_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_profile = _profile()
    missing_factory = _FakeClientFactory(response=_terminal_response())
    missing_provider = OpenAIResponsesProvider(missing_profile, client_factory=missing_factory)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ProviderInvocationError) as missing:
        missing_provider.invoke(
            _request(),
            control=_control(),
            credential_reference=_credential_reference(missing_profile),
        )

    assert missing.value.request_sent is False
    assert missing_factory.calls == []
    assert missing_provider.execution_provenance.real_inference_requests_sent == 0

    ambiguous_profile = _profile(api_key_env="OPENAI_API_KEY", api_key_file_env="OPENAI_API_KEY_FILE")
    key_file = tmp_path / "openai.key"
    key_file.write_text("sk-file-not-real\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-not-real")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key_file))
    ambiguous_factory = _FakeClientFactory(response=_terminal_response())
    ambiguous_provider = OpenAIResponsesProvider(ambiguous_profile, client_factory=ambiguous_factory)

    with pytest.raises(ProviderInvocationError) as ambiguous:
        ambiguous_provider.invoke(
            _request(),
            control=_control(),
            credential_reference=_credential_reference(ambiguous_profile),
        )

    assert ambiguous.value.request_sent is False
    assert "sk-env-not-real" not in str(ambiguous.value)
    assert ambiguous_factory.calls == []


def test_openai_responses_provider_preserves_usage_on_sent_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    response = _terminal_response()
    response["output_text"] = "not-json"
    factory = _FakeClientFactory(response=response)
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    with pytest.raises(ProviderInvocationError) as raised:
        provider.invoke(
            _request(),
            control=_control(),
            credential_reference=_credential_reference(profile),
        )

    assert raised.value.request_sent is True
    assert raised.value.usage is not None
    assert raised.value.usage.usage_status is UsageStatus.KNOWN
    assert raised.value.usage.cache_write_tokens == 100
    assert provider.execution_provenance == ProviderExecutionProvenance(
        execution_mode="live_provider",
        live_inference_status="failed",
        real_inference_requests_sent=1,
    )


def test_openai_responses_provider_unknown_usage_for_incomplete_response(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    factory = _FakeClientFactory(
        response=_terminal_response(status="incomplete", usage=None)
    )
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    with pytest.raises(ProviderInvocationError) as raised:
        provider.invoke(
            _request(),
            control=_control(),
            credential_reference=_credential_reference(profile),
        )

    assert raised.value.request_sent is True
    assert raised.value.usage is not None
    assert raised.value.usage.usage_status is UsageStatus.UNKNOWN
    assert raised.value.usage.cost_status is CostStatus.UNKNOWN
    assert provider.execution_provenance.live_inference_status == "failed"


def test_openai_responses_provider_requires_response_id_for_known_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    response = _terminal_response()
    del response["id"]
    factory = _FakeClientFactory(response=response)
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    invocation = provider.invoke(
        _request(),
        control=_control(),
        credential_reference=_credential_reference(profile),
    )

    assert invocation.usage.usage_status is UsageStatus.UNKNOWN
    assert invocation.usage.cost_status is CostStatus.UNKNOWN
    assert invocation.usage.response_id is None


def test_openai_responses_provider_disables_hidden_retries_and_marks_api_error_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    factory = _FakeClientFactory(error=RuntimeError("provider failed after dispatch"))
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    with pytest.raises(ProviderInvocationError) as raised:
        provider.invoke(
            _request(),
            control=_control(),
            credential_reference=_credential_reference(profile),
        )

    assert factory.calls[0]["max_retries"] == 0
    assert len(factory.client.payloads) == 1
    assert raised.value.request_sent is True
    assert provider.execution_provenance.real_inference_requests_sent == 1
    assert provider.execution_provenance.live_inference_status == "failed"


def test_openai_responses_provider_honors_pre_send_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    factory = _FakeClientFactory(response=_terminal_response())
    provider = OpenAIResponsesProvider(profile, client_factory=factory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    with pytest.raises(ProviderInvocationError) as raised:
        provider.invoke(
            _request(),
            control=_control(cancelled=True),
            credential_reference=_credential_reference(profile),
        )

    assert raised.value.request_sent is False
    assert raised.value.cancelled is True
    assert factory.calls == []
    assert provider.execution_provenance.live_inference_status == "not_run"


def test_default_registry_exposes_openai_without_arbitrary_config() -> None:
    assert default_harness_adapter_registry().provider_names == ("openai", "replay")
    assert default_harness_adapter_registry(
        allowed_provider_names=("replay",)
    ).provider_names == ("replay",)
