from __future__ import annotations

import pytest
from pydantic import ValidationError

from agintor.core.identity import canonical_identity_digest
from agintor.isolation.commands import IsolatedCommandPolicy
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessDeploymentProfileError,
    HarnessPromptCachePolicy,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
    harness_deployment_profile_digest,
)


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-harness-deployment-profile")


def _profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="openai.fixed.profile",
        provider="openai",
        model="gpt-4.1-mini",
        endpoint=HarnessProviderEndpoint(
            base_url_env="OPENAI_BASE_URL",
            api_key_env="OPENAI_API_KEY",
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=4096,
            reasoning_effort="medium",
        ),
        price_schedule=HarnessUsdPriceSchedule(
            input_usd_per_million_tokens=0.40,
            output_usd_per_million_tokens=1.60,
            cached_input_usd_per_million_tokens=0.0,
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "e" * 64,
            user="65532:65532",
            timeout_s=45.0,
            memory_bytes=768 * 1024 * 1024,
            cpu_count=1.5,
            pids_limit=192,
            output_bytes=2_000_000,
            tmpfs_bytes=96 * 1024 * 1024,
            nofile_limit=512,
            environment_allowlist=("TZ", "LANG", "PYTHONHASHSEED"),
        ),
    )


def _payload() -> dict:
    return _profile().profile_payload()


def test_profile_binds_all_digest_lanes_and_exact_deployment_identity() -> None:
    profile = _profile()
    identity = profile.to_deployment_identity()

    assert profile.runtime_kind == "harness"
    assert len(profile.provider_config_digest) == 64
    assert len(profile.decoding_policy_digest) == 64
    assert len(profile.price_schedule_digest) == 64
    assert len(profile.command_container_policy_digest) == 64
    assert identity.provider_config_digest == profile.provider_config_digest
    assert identity.decoding_policy_digest == profile.decoding_policy_digest
    assert identity.price_schedule_digest == profile.price_schedule_digest
    assert identity.command_container_policy_digest == profile.command_container_policy_digest
    assert harness_deployment_profile_digest(profile) == profile.profile_digest()
    profile.validate_deployment_identity(identity)

    crossed = identity.model_copy(
        update={"command_container_policy_digest": _digest("crossed-command-policy")}
    )
    with pytest.raises(HarnessDeploymentProfileError, match="deployment identity"):
        profile.validate_deployment_identity(crossed)


def test_command_container_policy_projects_exact_i0_policy() -> None:
    policy = _profile().command_container_policy.to_isolated_command_policy()

    assert policy == IsolatedCommandPolicy(
        image="python@sha256:" + "e" * 64,
        user="65532:65532",
        timeout_s=45.0,
        memory_bytes=768 * 1024 * 1024,
        cpu_count=1.5,
        pids_limit=192,
        output_bytes=2_000_000,
        tmpfs_bytes=96 * 1024 * 1024,
        nofile_limit=512,
        environment_allowlist=frozenset({"LANG", "PYTHONHASHSEED", "TZ"}),
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("image", "python:3.12", "pinned"),
        ("image", "python@sha256:" + "z" * 64, "pinned"),
        ("user", "0:65532", "non-root"),
        ("user", "65532:0", "non-root"),
        ("user", "app:app", "numeric"),
        ("environment_allowlist", ("LANG", "API_TOKEN"), "credential-like"),
    ],
)
def test_rejects_unpinned_root_or_secret_command_policy(field: str, value, match: str) -> None:
    payload = _payload()
    payload["command_container_policy"][field] = value

    with pytest.raises(ValidationError, match=match):
        HarnessDeploymentProfile.model_validate(payload)


def test_rejects_secret_values_paths_and_pricing_env_refs() -> None:
    payload = _payload()
    payload["endpoint"]["api_key_env"] = "sk-live-secret-value"
    with pytest.raises(ValidationError, match="environment variable name"):
        HarnessDeploymentProfile.model_validate(payload)

    payload = _payload()
    payload["endpoint"]["api_key_file_env"] = "C:/secret/key.txt"
    with pytest.raises(ValidationError, match="environment variable name"):
        HarnessDeploymentProfile.model_validate(payload)

    payload = _payload()
    payload["endpoint"] = {
        "base_url": "https://user:password@example.invalid/v1",
        "api_key_env": "OPENAI_API_KEY",
    }
    with pytest.raises(ValidationError, match="embedded credentials"):
        HarnessDeploymentProfile.model_validate(payload)

    payload = _payload()
    payload["endpoint"]["pricing_env"] = "OPENAI_PRICING"
    with pytest.raises(ValidationError, match="Extra inputs"):
        HarnessDeploymentProfile.model_validate(payload)


def test_rejects_digest_mismatch_and_profile_overrides() -> None:
    payload = _profile().model_dump(mode="python")
    payload["provider_config_digest"] = _digest("wrong-provider-config")

    with pytest.raises(ValidationError, match="provider_config_digest"):
        HarnessDeploymentProfile.model_validate(payload)

    profile = _profile()
    crossed_provider = profile.to_deployment_identity().model_copy(
        update={"provider": "other-provider"}
    )
    with pytest.raises(HarnessDeploymentProfileError, match="deployment identity"):
        profile.validate_deployment_identity(crossed_provider)


def test_rejects_unknown_zero_paid_pricing_and_requires_free_policy_justification() -> None:
    payload = _payload()
    payload["price_schedule"] = {
        "billing_mode": "paid",
        "input_usd_per_million_tokens": 0.0,
        "output_usd_per_million_tokens": 0.0,
        "cached_input_usd_per_million_tokens": 0.0,
    }
    with pytest.raises(ValidationError, match="positive input and output"):
        HarnessDeploymentProfile.model_validate(payload)

    payload = _payload()
    payload["price_schedule"] = {
        "billing_mode": "free",
        "input_usd_per_million_tokens": 0.0,
        "output_usd_per_million_tokens": 0.0,
        "cached_input_usd_per_million_tokens": 0.0,
    }
    with pytest.raises(ValidationError, match="justification"):
        HarnessDeploymentProfile.model_validate(payload)

    payload["price_schedule"]["provider_policy_justification"] = (
        "scripted fixture is not billed by any live provider"
    )
    free = HarnessDeploymentProfile.model_validate(payload)
    assert free.price_schedule.billing_mode == "free"
    assert free.price_schedule.input_usd_per_million_tokens == 0.0


def test_terra_none_profile_freezes_explicit_prompt_cache_and_standard_pricing() -> None:
    profile = _profile().model_copy(
        update={
            "deployment_id": "openai.gpt-5.6-terra.none.v1",
            "model": "gpt-5.6-terra",
            "decoding_policy": HarnessDecodingPolicy(
                temperature=0.0,
                top_p=1.0,
                max_output_tokens=256,
                reasoning_effort="none",
            ),
            "prompt_cache_policy": HarnessPromptCachePolicy(
                mode="explicit",
                prompt_cache_key="agintor:repo-repair-v1:terra-none",
                ttl="30m",
                breakpoint="static_prefix",
            ),
            "price_schedule": HarnessUsdPriceSchedule(
                input_usd_per_million_tokens=2.50,
                cached_input_usd_per_million_tokens=0.25,
                cache_write_usd_per_million_tokens=3.125,
                output_usd_per_million_tokens=15.00,
            ),
            "provider_config_digest": "",
            "decoding_policy_digest": "",
            "price_schedule_digest": "",
            "command_container_policy_digest": "",
        }
    )
    profile = HarnessDeploymentProfile.model_validate(profile.model_dump(mode="python"))

    assert profile.model == "gpt-5.6-terra"
    assert profile.decoding_policy.reasoning_effort == "none"
    assert profile.decoding_policy.service_tier == "default"
    assert profile.decoding_policy.store is False
    assert profile.decoding_policy.parallel_tool_calls is False
    assert profile.prompt_cache_policy.mode == "explicit"
    assert profile.prompt_cache_policy.minimum_prefix_tokens == 1024
    assert profile.prompt_cache_policy.maximum_breakpoints == 1
    assert profile.price_schedule.cache_write_usd_per_million_tokens == 3.125
    assert "prompt_cache_policy" in profile.provider_config_payload()


def test_explicit_prompt_cache_requires_complete_policy_and_write_price() -> None:
    payload = _payload()
    payload["prompt_cache_policy"] = {
        "mode": "explicit",
        "prompt_cache_key": "agintor:test",
        "ttl": "30m",
        "breakpoint": "static_prefix",
    }
    with pytest.raises(ValidationError, match="cache-write USD rate"):
        HarnessDeploymentProfile.model_validate(payload)

    payload["price_schedule"]["cache_write_usd_per_million_tokens"] = 3.125
    payload["prompt_cache_policy"].pop("prompt_cache_key")
    with pytest.raises(ValidationError, match="requires prompt_cache_key"):
        HarnessDeploymentProfile.model_validate(payload)
