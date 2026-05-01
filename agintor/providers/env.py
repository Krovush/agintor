from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from ..core.exceptions import AgintorError, ProviderConfigurationError, ProviderExhaustedError
from .base import (
    LocalDeterministicProvider,
    ModelProvider,
    load_api_key_from_file,
    load_openai_api_key_from_file,
    provider_kwargs_from_profile,
    resolve_api_key,
    resolve_openai_api_key,
)
from ..runtime.profile import HostedProviderProfile
from ..contracts import ModelRequest, ModelResponse, ReplayAllocation

_HOSTED_PROVIDER_NAMES = ("minimax", "openai")


def _normalize_provider_name(name: str) -> str:
    return str(name).strip().lower()


def _provider_defaults(name: str) -> dict[str, Any]:
    provider_name = _normalize_provider_name(name)
    if provider_name == "openai":
        from .openai import OPENAI_PROVIDER_DEFAULTS

        return OPENAI_PROVIDER_DEFAULTS
    if provider_name == "minimax":
        from .minimax import MINIMAX_PROVIDER_DEFAULTS

        return MINIMAX_PROVIDER_DEFAULTS
    return {}


def provider_profile_for_name(name: str, provider_profile: HostedProviderProfile | None) -> HostedProviderProfile | None:
    if provider_profile is None or _normalize_provider_name(provider_profile.name) != _normalize_provider_name(name):
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
        api_key_file_env = _provider_defaults(name).get("api_key_file_env")
    return str(api_key_file_env) if api_key_file_env else None


def provider_environment_names(
    name: str,
    *,
    provider_profile: HostedProviderProfile | None = None,
    include_api_key_file_env: bool = False,
) -> list[str]:
    provider_profile = provider_profile_for_name(name, provider_profile)
    defaults = _provider_defaults(name)
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
    def collect(instance: Any, env_names: set[str], visited: set[int]) -> None:
        ident = id(instance)
        if ident in visited:
            return
        visited.add(ident)
        for attr in ("api_key_env", "base_url_env", "pricing_env"):
            value = getattr(instance, attr, None)
            if value:
                env_names.add(str(value))
        model_envs = getattr(instance, "model_envs", None)
        if isinstance(model_envs, dict):
            env_names.update(str(value) for value in model_envs.values() if value)
        if include_api_key_file_env:
            api_key_file_env = getattr(instance, "api_key_file_env", None)
            if api_key_file_env:
                env_names.add(str(api_key_file_env))
        wrapped = getattr(instance, "wrapped", None)
        if wrapped is not None:
            collect(wrapped, env_names, visited)
        providers = getattr(instance, "providers", None)
        if isinstance(providers, list):
            for child in providers:
                collect(child, env_names, visited)

    env_names: set[str] = set()
    collect(provider, env_names, set())
    return sorted(env_name for env_name in env_names if env_name)


def known_provider_environment_names(*, include_api_key_file_env: bool = False) -> list[str]:
    env_names: set[str] = set()
    for provider_name in _HOSTED_PROVIDER_NAMES:
        env_names.update(
            provider_environment_names(
                provider_name,
                include_api_key_file_env=include_api_key_file_env,
            )
        )
    return sorted(env_names)
