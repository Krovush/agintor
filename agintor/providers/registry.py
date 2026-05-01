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

from .env import _normalize_provider_name, provider_profile_for_name
from .failover import FailoverProvider
from .payloads import (
    _copy_json_like,
    provider_payload,
)
from .replay import ReplayProvider
from .retry import RetryProvider

_PROVIDER_KWARG_NAMES: dict[str, set[str]] = {
    "local": set(),
    "openai": {
        "reasoning_effort_map",
        "api_key",
        "api_key_file",
        "base_url",
        "base_url_env",
        "api_key_env",
        "api_key_file_env",
        "model_map",
        "model_envs",
        "default_models",
        "temperature",
        "pricing_map",
        "pricing_env",
    },
    "minimax": {
        "api_key",
        "api_key_file",
        "base_url",
        "base_url_env",
        "api_key_env",
        "api_key_file_env",
        "model_map",
        "model_envs",
        "default_models",
        "temperature",
        "pricing_map",
        "pricing_env",
    },
    "replay": {"replay_file", "rows", "cursor", "cursor_start", "cursor_end"},
}


def _provider_kwargs_for(name: str, provider_profile: HostedProviderProfile | None, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    provider_name = _normalize_provider_name(name)
    supported = _PROVIDER_KWARG_NAMES.get(provider_name, set())
    selected = {key: value for key, value in kwargs.items() if key in supported and value is not None}
    if provider_name in {"openai", "minimax"}:
        profile_kwargs = provider_kwargs_from_profile(provider_profile)
        selected = {**{key: value for key, value in profile_kwargs.items() if key in supported and value is not None}, **selected}
    return selected


def _validate_provider_kwargs(provider_names: list[str], kwargs: Mapping[str, Any]) -> None:
    supported: set[str] = set()
    for provider_name in provider_names:
        supported.update(_PROVIDER_KWARG_NAMES.get(_normalize_provider_name(provider_name), set()))
    unknown = sorted(key for key, value in kwargs.items() if value is not None and key not in supported)
    if unknown:
        joined = ", ".join(unknown)
        providers = ", ".join(sorted({_normalize_provider_name(name) for name in provider_names}))
        raise TypeError(f"unsupported provider kwargs for {providers}: {joined}")


def __getattr__(name: str) -> Any:
    if name in {"OpenAIProvider", "OPENAI_PROVIDER_DEFAULTS"}:
        from .openai import OPENAI_PROVIDER_DEFAULTS, OpenAIProvider

        return {"OpenAIProvider": OpenAIProvider, "OPENAI_PROVIDER_DEFAULTS": OPENAI_PROVIDER_DEFAULTS}[name]
    if name in {"MiniMaxProvider", "MINIMAX_PROVIDER_DEFAULTS"}:
        from .minimax import MINIMAX_PROVIDER_DEFAULTS, MiniMaxProvider

        return {"MiniMaxProvider": MiniMaxProvider, "MINIMAX_PROVIDER_DEFAULTS": MINIMAX_PROVIDER_DEFAULTS}[name]
    raise AttributeError(name)


def build_provider_from_payload(
    payload: Mapping[str, Any],
    *,
    provider_profile: HostedProviderProfile | None = None,
) -> ModelProvider:
    kind = _normalize_provider_name(str(payload.get("kind", "")))
    if kind == "retry":
        wrapped = payload.get("wrapped")
        if not isinstance(wrapped, Mapping):
            raise ProviderConfigurationError("retry provider payload requires wrapped provider config")
        return RetryProvider(
            build_provider_from_payload(wrapped, provider_profile=provider_profile),
            max_retries=int(payload.get("max_retries", 0) or 0),
            backoff_s=float(payload.get("backoff_s", 0.25) or 0.0),
            max_backoff_s=float(payload.get("max_backoff_s", 2.0) or 0.0),
        )
    if kind == "failover":
        providers_payload = payload.get("providers")
        if not isinstance(providers_payload, list) or not providers_payload:
            raise ProviderConfigurationError("failover provider payload requires at least one child provider")
        providers: list[ModelProvider] = []
        for item in providers_payload:
            if not isinstance(item, Mapping):
                raise ProviderConfigurationError("failover provider payload children must be objects")
            providers.append(build_provider_from_payload(item, provider_profile=provider_profile))
        return FailoverProvider(providers)
    kwargs = {
        key: _copy_json_like(value)
        for key, value in payload.items()
        if key != "kind"
    }
    return _build_single_provider(kind, provider_profile=provider_profile, **kwargs)


def _build_single_provider(
    name: str,
    *,
    provider_profile: HostedProviderProfile | None = None,
    **kwargs: Any,
) -> ModelProvider:
    provider_name = _normalize_provider_name(name)
    provider_profile = provider_profile_for_name(provider_name, provider_profile)
    provider_kwargs = _provider_kwargs_for(provider_name, provider_profile, kwargs)
    if provider_name == "local":
        return LocalDeterministicProvider()
    if provider_name == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(**provider_kwargs)
    if provider_name == "minimax":
        from .minimax import MiniMaxProvider

        return MiniMaxProvider(**provider_kwargs)
    if provider_name == "replay":
        replay_file = provider_kwargs.get("replay_file")
        rows = provider_kwargs.get("rows")
        cursor = int(provider_kwargs.get("cursor", 0) or 0)
        cursor_start = provider_kwargs.get("cursor_start")
        cursor_end = provider_kwargs.get("cursor_end")
        if replay_file:
            return ReplayProvider.from_file(
                replay_file,
                cursor=cursor,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
            )
        if isinstance(rows, list):
            return ReplayProvider(
                [dict(row) for row in rows],
                cursor=cursor,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
            )
        raise ProviderConfigurationError("Replay provider requires replay_file=<path> or inline rows")
    raise ValueError(f"unknown provider {name}")


def build_provider(
    name: str = "local",
    *,
    provider_profile: HostedProviderProfile | None = None,
    max_retries: int = 0,
    retry_backoff_s: float = 0.25,
    fallback_names: list[str] | None = None,
    **kwargs: Any,
) -> ModelProvider:
    normalized_name = _normalize_provider_name(name)
    normalized_fallbacks = [_normalize_provider_name(fallback_name) for fallback_name in list(fallback_names or [])]
    _validate_provider_kwargs([normalized_name, *normalized_fallbacks], kwargs)

    primary = _build_single_provider(normalized_name, provider_profile=provider_profile, **kwargs)
    if max_retries > 0 and normalized_name != "local":
        primary = RetryProvider(primary, max_retries=max_retries, backoff_s=retry_backoff_s)
    if not normalized_fallbacks:
        return primary
    providers: list[ModelProvider] = [primary]
    for fallback_name in normalized_fallbacks:
        fallback = _build_single_provider(fallback_name, provider_profile=provider_profile, **kwargs)
        if max_retries > 0 and fallback_name != "local":
            fallback = RetryProvider(fallback, max_retries=max_retries, backoff_s=retry_backoff_s)
        providers.append(fallback)
    return FailoverProvider(providers)


def clone_provider(
    provider: ModelProvider,
    *,
    provider_profile: HostedProviderProfile | None = None,
) -> ModelProvider:
    if isinstance(provider, ReplayProvider):
        return provider.shared_clone()
    if isinstance(provider, RetryProvider):
        return RetryProvider(
            clone_provider(provider.wrapped, provider_profile=provider_profile),
            max_retries=provider.max_retries,
            backoff_s=provider.backoff_s,
            max_backoff_s=provider.max_backoff_s,
        )
    if isinstance(provider, FailoverProvider):
        return FailoverProvider(
            [clone_provider(child, provider_profile=provider_profile) for child in provider.providers]
        )
    return build_provider_from_payload(provider_payload(provider), provider_profile=provider_profile)
