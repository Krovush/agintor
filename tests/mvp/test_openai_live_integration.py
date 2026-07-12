from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

from agintor.contracts.epochs import TaskCeilings
from agintor.core.identity import canonical_identity_digest
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
    ProviderCallResult,
    ProviderCallStatus,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallRequest,
    ActorTerminalTurn,
    ActorToolRequest,
    PreCallContextManifest,
)
from agintor.runtime.kernel.openai_responses_provider import OpenAIResponsesProvider
from agintor.utils import count_tokens_rough


LIVE_ENABLE_ENV = "AGINTOR_ENABLE_LIVE_OPENAI"
LIVE_KEY_FILE_ENV = "AGINTOR_LIVE_OPENAI_KEY_FILE"
COMBINED_LIVE_CAP_USD = 1.00
TERRA_LIVE_CAP_USD = 0.75

LUNA_MODEL = "gpt-5.6-luna"
TERRA_MODEL = "gpt-5.6-terra"

pytestmark = [
    pytest.mark.live_openai,
    pytest.mark.skipif(
        os.environ.get(LIVE_ENABLE_ENV) != "1",
        reason=f"set {LIVE_ENABLE_ENV}=1 to run bounded live OpenAI tests",
    ),
]


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-openai-live-integration")


def _command_policy() -> HarnessCommandContainerPolicy:
    return HarnessCommandContainerPolicy(
        image="python@sha256:" + "d" * 64,
        timeout_s=30.0,
        memory_bytes=256 * 1024 * 1024,
        cpu_count=1.0,
        pids_limit=128,
        output_bytes=1_000_000,
        tmpfs_bytes=64 * 1024 * 1024,
        nofile_limit=256,
    )


def _price_schedule(model: str) -> HarnessUsdPriceSchedule:
    if model == LUNA_MODEL:
        return HarnessUsdPriceSchedule(
            billing_mode="paid",
            input_usd_per_million_tokens=1.0,
            output_usd_per_million_tokens=6.0,
            cached_input_usd_per_million_tokens=0.10,
            cache_write_usd_per_million_tokens=1.25,
        )
    if model == TERRA_MODEL:
        return HarnessUsdPriceSchedule(
            billing_mode="paid",
            input_usd_per_million_tokens=2.50,
            output_usd_per_million_tokens=15.0,
            cached_input_usd_per_million_tokens=0.25,
            cache_write_usd_per_million_tokens=3.125,
        )
    raise ValueError(f"unexpected live OpenAI model {model!r}")


def _profile(
    *,
    model: str,
    cache_key: str | None,
    max_output_tokens: int,
) -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id=f"openai.{model}.none.standard.live-test",
        provider="openai",
        model=model,
        endpoint=HarnessProviderEndpoint(
            base_url="https://api.openai.com/v1",
            api_key_env=None,
            api_key_file_env=LIVE_KEY_FILE_ENV,
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=max_output_tokens,
            reasoning_effort="none",
            service_tier="default",
            store=False,
            parallel_tool_calls=False,
            text_verbosity="low",
        ),
        prompt_cache_policy=(
            HarnessPromptCachePolicy(
                mode="explicit",
                prompt_cache_key=cache_key,
                ttl="30m",
                breakpoint="static_prefix",
            )
            if cache_key is not None
            else HarnessPromptCachePolicy()
        ),
        price_schedule=_price_schedule(model),
        command_container_policy=_command_policy(),
    )


def _request(
    *,
    label: str,
    instruction: str,
    allowed_tool_ids: tuple[str, ...] = ("repo.read",),
    max_output_tokens: int = 160,
) -> ActorCallRequest:
    context = PreCallContextManifest(
        call_id=f"call.{label}",
        actor_id="actor.live_openai",
        task_envelope_digest=_digest("live-openai-task"),
        reads=(),
    )
    return ActorCallRequest(
        run_id="run.live-openai",
        compiled_semantic_digest=_digest("compiled-live-openai"),
        call_id=f"call.{label}",
        actor_id="actor.live_openai",
        call_kind="initial",
        instruction=instruction,
        allowed_tool_ids=allowed_tool_ids,
        budget_share_bps=10_000,
        context=context,
        input_token_estimate=2_000,
        max_output_tokens=max_output_tokens,
    )


def _credential_reference(profile: HarnessDeploymentProfile) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=profile.endpoint.api_key_env,
        api_key_file_env=profile.endpoint.api_key_file_env,
    )


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=3,
        max_input_tokens=36_000,
        max_output_tokens=480,
        max_cached_tokens=18_000,
        max_cache_write_tokens=18_000,
        max_tool_calls=1,
        max_tool_output_bytes=1_000,
        max_artifact_bytes=1_000,
        max_patch_bytes=1_000,
        max_retries=0,
        max_wall_time_ms=180_000,
        provider_deadline_ms=60_000,
        max_known_cost_usd=COMBINED_LIVE_CAP_USD,
        max_estimated_cost_usd=COMBINED_LIVE_CAP_USD,
    )


def _worst_case_cost(
    profile: HarnessDeploymentProfile,
    *,
    input_tokens: int,
    max_output_tokens: int,
) -> float:
    schedule = profile.price_schedule
    input_rate = max(
        schedule.input_usd_per_million_tokens,
        schedule.cache_write_usd_per_million_tokens,
    )
    return (
        input_tokens * input_rate
        + max_output_tokens * schedule.output_usd_per_million_tokens
    ) / 1_000_000


class _CapturingResponses:
    def __init__(self, inner: Any, payloads: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._payloads = payloads

    def create(self, **payload: Any) -> Any:
        self._payloads.append(json.loads(json.dumps(payload)))
        return self._inner.create(**payload)


class _CapturingClient:
    def __init__(self, inner: Any, payloads: list[dict[str, Any]]) -> None:
        self.responses = _CapturingResponses(inner.responses, payloads)


class _CapturingClientFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _CapturingClient:
        from openai import OpenAI

        api_key = kwargs.pop("api_key")
        self.calls.append(
            {
                "base_url": str(kwargs.get("base_url")),
                "timeout": kwargs.get("timeout"),
                "max_retries": kwargs.get("max_retries"),
                "api_key_present": bool(api_key),
            }
        )
        client = OpenAI(api_key=api_key, **kwargs)
        return _CapturingClient(client, self.payloads)


def _require_live_key_file_env() -> str:
    key_file = os.environ.get(LIVE_KEY_FILE_ENV)
    if not key_file or not key_file.strip():
        pytest.skip(f"set {LIVE_KEY_FILE_ENV} to the OpenAI key-file path")
    return key_file.strip()


def _assert_cacheable_static_prefix(
    provider: OpenAIResponsesProvider,
    request: ActorCallRequest,
) -> None:
    payload = provider._response_payload(request)
    assert payload["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert payload["input"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }
    static_prefix = (
        payload["instructions"] + "\n" + payload["input"][0]["content"][0]["text"]
    )
    assert count_tokens_rough(static_prefix) >= 1024, (
        "explicit prompt-cache writes require the provider static prefix at "
        "the breakpoint to be at least 1,024 tokens"
    )


def _call_provider(
    controller: CompositeProviderController,
    provider: OpenAIResponsesProvider,
    request: ActorCallRequest,
    profile: HarnessDeploymentProfile,
    *,
    input_tokens: int = 12_000,
    max_output_tokens: int = 160,
    reserve_cache: bool,
) -> ProviderCallResult:
    cache_reservation = input_tokens // 2 if reserve_cache else 0
    return controller.call(
        provider,
        request,
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        max_cached_tokens=cache_reservation,
        max_cache_write_tokens=cache_reservation,
        estimated_cost_usd=_worst_case_cost(
            profile,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
        ),
        expected_cost_status=CostStatus.KNOWN,
        is_retry=False,
        credential_reference=_credential_reference(profile),
    )


def _known_success(result: ProviderCallResult) -> ProviderCallResult:
    assert result.status is ProviderCallStatus.SUCCEEDED, result.model_dump(mode="json")
    assert result.invocation is not None
    usage = result.invocation.usage
    assert usage.usage_status is UsageStatus.KNOWN
    assert usage.cost_status is CostStatus.KNOWN
    assert usage.cost_usd is not None
    assert usage.response_id
    return result


def _assert_not_serialized(serialized: str, sensitive: str) -> None:
    assert sensitive not in serialized
    assert json.dumps(sensitive)[1:-1] not in serialized


def _assert_payloads_are_safe_and_frozen(
    factory: _CapturingClientFactory,
    *,
    key_file_path: str,
) -> None:
    assert [call["api_key_present"] for call in factory.calls] == [True, True, True]
    assert [call["max_retries"] for call in factory.calls] == [0, 0, 0]
    assert all(call["base_url"] == "https://api.openai.com/v1" for call in factory.calls)

    payload_json = json.dumps(factory.payloads, sort_keys=True)
    _assert_not_serialized(payload_json, key_file_path)
    assert LIVE_KEY_FILE_ENV not in payload_json

    models = [payload["model"] for payload in factory.payloads]
    assert models == [LUNA_MODEL, LUNA_MODEL, TERRA_MODEL]
    for payload in factory.payloads:
        assert payload["reasoning"] == {"effort": "none"}
        assert payload["service_tier"] == "default"
        assert payload["store"] is False
        assert payload["parallel_tool_calls"] is False
        assert payload["text"]["verbosity"] == "low"
        assert payload["text"]["format"]["strict"] is True
        assert "api_key" not in payload_json.lower()


def test_live_openai_luna_cache_and_terra_tool_behavior() -> None:
    pytest.importorskip("openai")
    key_file_path = _require_live_key_file_env()

    cache_key = f"agintor:live:luna:{uuid.uuid4().hex[:24]}"
    luna_profile = _profile(
        model=LUNA_MODEL,
        cache_key=cache_key,
        max_output_tokens=160,
    )
    terra_profile = _profile(
        model=TERRA_MODEL,
        cache_key=None,
        max_output_tokens=160,
    )
    factory = _CapturingClientFactory()
    luna_provider = OpenAIResponsesProvider(luna_profile, client_factory=factory)
    terra_provider = OpenAIResponsesProvider(terra_profile, client_factory=factory)

    luna_write_request = _request(
        label="luna_write",
        instruction=(
            "This is a low-intelligence transport and cache-write check. "
            "Return a terminal turn whose output_text is exactly "
            "'luna-cache-seed'. Do not request tools and do not emit a patch."
        ),
    )
    luna_read_request = _request(
        label="luna_read",
        instruction=(
            "This is a low-intelligence cache-read check using the same static "
            "prefix. Return a terminal turn whose output_text is exactly "
            "'luna-cache-read'. Do not request tools and do not emit a patch."
        ),
    )
    terra_tool_request = _request(
        label="terra_tool",
        instruction=(
            "A repository repair task reports a parser failure and the likely "
            "source file is src/parser.py. Before proposing any patch, inspect "
            "the code by returning exactly one typed tool_request for repo.read "
            "against src/parser.py. Do not return terminal output yet."
        ),
        allowed_tool_ids=("repo.read",),
    )

    _assert_cacheable_static_prefix(luna_provider, luna_write_request)

    luna_estimate = _worst_case_cost(
        luna_profile,
        input_tokens=12_000,
        max_output_tokens=160,
    )
    terra_estimate = _worst_case_cost(
        terra_profile,
        input_tokens=12_000,
        max_output_tokens=160,
    )
    assert luna_estimate * 2 + terra_estimate <= COMBINED_LIVE_CAP_USD
    assert terra_estimate <= TERRA_LIVE_CAP_USD

    controller = CompositeProviderController(AggregateBudgetLedger(_ceilings()))
    luna_write = _known_success(
        _call_provider(
            controller,
            luna_provider,
            luna_write_request,
            luna_profile,
            reserve_cache=True,
        )
    )
    luna_read = _known_success(
        _call_provider(
            controller,
            luna_provider,
            luna_read_request,
            luna_profile,
            reserve_cache=True,
        )
    )
    terra = _known_success(
        _call_provider(
            controller,
            terra_provider,
            terra_tool_request,
            terra_profile,
            reserve_cache=False,
        )
    )

    assert isinstance(luna_write.invocation.response, ActorTerminalTurn)
    assert luna_write.invocation.response.output.output_text == "luna-cache-seed"
    assert isinstance(luna_read.invocation.response, ActorTerminalTurn)
    assert luna_read.invocation.response.output.output_text == "luna-cache-read"
    assert luna_write.invocation.usage.cache_write_tokens > 0
    assert luna_read.invocation.usage.cached_tokens > 0

    assert isinstance(terra.invocation.response, ActorToolRequest)
    assert terra.invocation.response.tool_id == "repo.read"
    assert "parser.py" in json.dumps(terra.invocation.response.arguments).lower()
    assert terra.invocation.usage.cost_usd <= TERRA_LIVE_CAP_USD

    response_ids = [
        luna_write.invocation.usage.response_id,
        luna_read.invocation.usage.response_id,
        terra.invocation.usage.response_id,
    ]
    assert len(set(response_ids)) == 3
    assert luna_provider.execution_provenance.real_inference_requests_sent == 2
    assert luna_provider.execution_provenance.live_inference_status == "completed"
    assert terra_provider.execution_provenance.real_inference_requests_sent == 1
    assert terra_provider.execution_provenance.live_inference_status == "completed"

    snapshot = controller.ledger.snapshot()
    assert snapshot.model_calls == 3
    assert snapshot.retries == 0
    assert snapshot.known_cost_usd <= COMBINED_LIVE_CAP_USD
    assert snapshot.estimated_cost_usd == 0.0
    assert snapshot.unknown_cost_events == 0
    assert snapshot.unknown_usage_events == 0
    assert snapshot.healthy is True
    assert snapshot.reconciled is True

    serialized_results = json.dumps(
        [
            luna_write.model_dump(mode="json"),
            luna_read.model_dump(mode="json"),
            terra.model_dump(mode="json"),
            snapshot.model_dump(mode="json"),
        ],
        sort_keys=True,
    )
    _assert_not_serialized(serialized_results, key_file_path)
    assert LIVE_KEY_FILE_ENV not in serialized_results
    _assert_payloads_are_safe_and_frozen(factory, key_file_path=key_file_path)
    print(
        json.dumps(
            {
                "combined_cost_usd": snapshot.known_cost_usd,
                "luna_cost_usd": (
                    luna_write.invocation.usage.cost_usd
                    + luna_read.invocation.usage.cost_usd
                ),
                "luna_cache_write_tokens": (
                    luna_write.invocation.usage.cache_write_tokens
                ),
                "luna_cached_tokens": luna_read.invocation.usage.cached_tokens,
                "terra_cost_usd": terra.invocation.usage.cost_usd,
                "terra_model": TERRA_MODEL,
                "terra_reasoning_effort": "none",
            },
            sort_keys=True,
        )
    )
