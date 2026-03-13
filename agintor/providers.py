from __future__ import annotations

from typing import Any

from .provider_common import (
    LocalDeterministicProvider,
    ModelProvider,
    load_api_key_from_file,
    load_openai_api_key_from_file,
    provider_kwargs_from_profile,
    resolve_api_key,
    resolve_openai_api_key,
)
from .provider_minimax import MINIMAX_PROVIDER_DEFAULTS, MiniMaxProvider
from .provider_openai import OPENAI_PROVIDER_DEFAULTS, OpenAIProvider
from .runtime_profile import HostedProviderProfile


_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": OPENAI_PROVIDER_DEFAULTS,
    "minimax": MINIMAX_PROVIDER_DEFAULTS,
}


def provider_profile_for_name(name: str, provider_profile: HostedProviderProfile | None) -> HostedProviderProfile | None:
    if provider_profile is None or provider_profile.name != name:
        return None
    return provider_profile


def provider_api_key_file_env_name(
    name: str,
    *,
    provider_profile: HostedProviderProfile | None = None,
) -> str | None:
    provider_profile = provider_profile_for_name(name, provider_profile)
    api_key_file_env = provider_profile.api_key_file_env if provider_profile is not None else None
    if not api_key_file_env:
        api_key_file_env = _PROVIDER_DEFAULTS.get(name, {}).get("api_key_file_env")
    return str(api_key_file_env) if api_key_file_env else None


def build_provider(
    name: str = "local",
    *,
    provider_profile: HostedProviderProfile | None = None,
    **kwargs: Any,
) -> ModelProvider:
    provider_profile = provider_profile_for_name(name, provider_profile)
    if name == "local":
        return LocalDeterministicProvider()
    if name == "openai":
        return OpenAIProvider(**{**provider_kwargs_from_profile(provider_profile), **kwargs})
    if name == "minimax":
        return MiniMaxProvider(**{**provider_kwargs_from_profile(provider_profile), **kwargs})
    raise ValueError(f"unknown provider {name}")


def provider_environment_names(
    name: str,
    *,
    provider_profile: HostedProviderProfile | None = None,
    include_api_key_file_env: bool = False,
) -> list[str]:
    provider_profile = provider_profile_for_name(name, provider_profile)
    defaults = _PROVIDER_DEFAULTS.get(name, {})
    env_names: set[str] = set()
    for key in ("api_key_env", "base_url_env", "pricing_env"):
        value = getattr(provider_profile, key, None) if provider_profile is not None else None
        if not value:
            value = defaults.get(key)
        if value:
            env_names.add(str(value))
    env_names.update(str(value) for value in dict(defaults.get("model_envs", {})).values() if value)
    if include_api_key_file_env:
        api_key_file_env = provider_api_key_file_env_name(
            name,
            provider_profile=provider_profile,
        )
        if api_key_file_env:
            env_names.add(str(api_key_file_env))
    return sorted(env_name for env_name in env_names if env_name)


def provider_environment_names_for_instance(
    provider: ModelProvider,
    *,
    include_api_key_file_env: bool = False,
) -> list[str]:
    env_names: set[str] = set()
    for attr in ("api_key_env", "base_url_env", "pricing_env"):
        value = getattr(provider, attr, None)
        if value:
            env_names.add(str(value))
    model_envs = getattr(provider, "model_envs", None)
    if isinstance(model_envs, dict):
        env_names.update(str(value) for value in model_envs.values() if value)
    if include_api_key_file_env:
        api_key_file_env = getattr(provider, "api_key_file_env", None)
        if api_key_file_env:
            env_names.add(str(api_key_file_env))
    return sorted(env_name for env_name in env_names if env_name)


def known_provider_environment_names(*, include_api_key_file_env: bool = False) -> list[str]:
    env_names: set[str] = set()
    for provider_name in sorted(_PROVIDER_DEFAULTS):
        env_names.update(
            provider_environment_names(
                provider_name,
                include_api_key_file_env=include_api_key_file_env,
            )
        )
    return sorted(env_names)


__all__ = [
    "LocalDeterministicProvider",
    "MiniMaxProvider",
    "ModelProvider",
    "OpenAIProvider",
    "build_provider",
    "known_provider_environment_names",
    "load_api_key_from_file",
    "load_openai_api_key_from_file",
    "provider_environment_names",
    "provider_environment_names_for_instance",
    "provider_api_key_file_env_name",
    "provider_profile_for_name",
    "resolve_api_key",
    "resolve_openai_api_key",
]
