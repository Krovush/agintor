from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from .exceptions import AgintorError
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
from .schemas import ModelRequest, ModelResponse


_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": OPENAI_PROVIDER_DEFAULTS,
    "minimax": MINIMAX_PROVIDER_DEFAULTS,
}


class ReplayProvider(ModelProvider):
    """Offline replay provider for deterministic hosted-provider emulation."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__("replay")
        self._rows = [dict(row) for row in rows]
        self._cursor = 0

    @classmethod
    def from_file(cls, path: str | Path) -> "ReplayProvider":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise AgintorError("Replay provider file must contain a JSON array of response rows")
        return cls([row for row in payload if isinstance(row, Mapping)])

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._cursor >= len(self._rows):
            raise AgintorError("Replay provider exhausted: no more recorded responses")
        row = dict(self._rows[self._cursor])
        self._cursor += 1
        response = ModelResponse(
            text=str(row.get("text", "")),
            raw={
                "provider": "replay",
                "record_index": self._cursor - 1,
                "request_model_class": request.model_class,
            },
            model_name=str(row.get("model_name", f"replay/{request.model_class}")),
            input_tokens=int(row.get("input_tokens", 0) or 0),
            output_tokens=int(row.get("output_tokens", 0) or 0),
            token_estimate=int(row.get("token_estimate", 0) or 0),
            latency_s=float(row.get("latency_s", 0.0) or 0.0),
            dollar_cost=float(row.get("dollar_cost", 0.0) or 0.0),
        )
        self._record_usage(response)
        return response


class RetryProvider(ModelProvider):
    """Retry wrapper for hosted providers with basic transient-error handling and audit events."""

    def __init__(
        self,
        wrapped: ModelProvider,
        *,
        max_retries: int = 2,
        backoff_s: float = 0.25,
        max_backoff_s: float = 2.0,
    ) -> None:
        super().__init__(getattr(wrapped, "provider_name", "wrapped"))
        self.wrapped = wrapped
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = max(0.0, float(backoff_s))
        self.max_backoff_s = max(0.0, float(max_backoff_s))
        self._audit_events: list[dict[str, Any]] = []

    def _is_retryable(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(token in text for token in ("rate", "429", "timeout", "temporar", "connection", "503", "unavailable"))

    def _record_audit(self, event: str, **fields: Any) -> None:
        self._audit_events.append({"ts": time.time(), "event": event, **fields})

    def audit_trail(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._audit_events]

    def health_check(self) -> dict[str, Any]:
        api_key = getattr(self.wrapped, "api_key", None)
        return {
            "provider": getattr(self.wrapped, "provider_name", "unknown"),
            "ok": bool(api_key) or getattr(self.wrapped, "provider_name", "local") == "local",
            "has_api_key": bool(api_key),
        }

    def generate(self, request: ModelRequest) -> ModelResponse:
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self.wrapped.generate(request)
                self._record_usage(response)
                self._record_audit("success", attempt=attempt, model_class=request.model_class)
                return response
            except Exception as exc:
                retryable = self._is_retryable(exc)
                self._record_audit("error", attempt=attempt, retryable=retryable, error=str(exc))
                if attempt >= attempts or not retryable:
                    raise
                sleep_s = min(self.max_backoff_s, self.backoff_s * (2 ** (attempt - 1)))
                if sleep_s > 0:
                    time.sleep(sleep_s)
        raise AgintorError("Retry provider exhausted unexpectedly")


class FailoverProvider(ModelProvider):
    """Attempt providers in order and fail over on hard provider errors."""

    def __init__(self, providers: list[ModelProvider]) -> None:
        if not providers:
            raise AgintorError("FailoverProvider requires at least one provider")
        super().__init__(getattr(providers[0], "provider_name", "failover"))
        self.providers = providers
        self._last_failures: list[str] = []

    def last_failures(self) -> list[str]:
        return list(self._last_failures)

    def usage_summary(self) -> dict[str, Any]:
        total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "dollar_cost": 0.0}
        for provider in self.providers:
            usage = provider.usage_summary()
            for key in total:
                total[key] += usage.get(key, 0)
        return total

    def generate(self, request: ModelRequest) -> ModelResponse:
        failures: list[str] = []
        for provider in self.providers:
            try:
                response = provider.generate(request)
                self._record_usage(response)
                self._last_failures = failures
                return response
            except Exception as exc:
                failures.append(f"{getattr(provider, 'provider_name', provider.__class__.__name__)}: {exc}")
                continue
        self._last_failures = failures
        joined = " | ".join(failures) if failures else "no providers tried"
        raise AgintorError(f"All providers failed: {joined}")


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


def _build_single_provider(
    name: str,
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
    if name == "replay":
        replay_file = kwargs.get("replay_file")
        if not replay_file:
            raise AgintorError("Replay provider requires replay_file=<path>")
        return ReplayProvider.from_file(replay_file)
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
    primary = _build_single_provider(name, provider_profile=provider_profile, **kwargs)
    if max_retries > 0 and name != "local":
        primary = RetryProvider(primary, max_retries=max_retries, backoff_s=retry_backoff_s)
    if not fallback_names:
        return primary
    providers: list[ModelProvider] = [primary]
    for fallback_name in fallback_names:
        fallback = _build_single_provider(fallback_name, provider_profile=provider_profile, **kwargs)
        if max_retries > 0 and fallback_name != "local":
            fallback = RetryProvider(fallback, max_retries=max_retries, backoff_s=retry_backoff_s)
        providers.append(fallback)
    return FailoverProvider(providers)


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
    for provider_name in sorted(_PROVIDER_DEFAULTS):
        env_names.update(
            provider_environment_names(
                provider_name,
                include_api_key_file_env=include_api_key_file_env,
            )
        )
    return sorted(env_names)


__all__ = [
    "FailoverProvider",
    "LocalDeterministicProvider",
    "MiniMaxProvider",
    "ModelProvider",
    "OpenAIProvider",
    "ReplayProvider",
    "RetryProvider",
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
