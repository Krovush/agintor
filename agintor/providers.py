from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from .exceptions import AgintorError, ProviderConfigurationError, ProviderExhaustedError
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

_RETRYABLE_ERROR_TOKENS = ("rate", "429", "timeout", "temporar", "connection", "503", "unavailable")
_FAILOVER_ERROR_TOKENS = _RETRYABLE_ERROR_TOKENS + ("overloaded", "bad gateway", "gateway timeout")
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
    "replay": {"replay_file"},
}
_PROVIDER_FILE_KEYS = {"api_key_file", "replay_file"}


def _normalize_provider_name(name: str) -> str:
    return str(name).strip().lower()


def _provider_error_text(exc: Exception) -> str:
    return str(exc).strip().lower()


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, ProviderExhaustedError):
        return False
    text = _provider_error_text(exc)
    return any(token in text for token in _RETRYABLE_ERROR_TOKENS)


def _is_failoverable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, ProviderExhaustedError):
        return False
    text = _provider_error_text(exc)
    return any(token in text for token in _FAILOVER_ERROR_TOKENS)


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


class ReplayProvider(ModelProvider):
    """Offline replay provider for deterministic hosted-provider emulation."""

    def __init__(self, rows: list[dict[str, Any]], *, replay_file: str | None = None) -> None:
        super().__init__("replay")
        self._rows = [dict(row) for row in rows]
        self._cursor = 0
        self.replay_file = str(replay_file) if replay_file else None

    @classmethod
    def from_file(cls, path: str | Path) -> "ReplayProvider":
        replay_path = str(Path(path))
        try:
            payload = json.loads(Path(replay_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderConfigurationError(f"Replay provider file is not readable: {replay_path}") from exc
        if not isinstance(payload, list):
            raise ProviderConfigurationError("Replay provider file must contain a JSON array of response rows")
        if any(not isinstance(row, Mapping) for row in payload):
            raise ProviderConfigurationError("Replay provider rows must be JSON objects")
        return cls([dict(row) for row in payload], replay_file=replay_path)

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._cursor >= len(self._rows):
            raise ProviderExhaustedError("Replay provider exhausted: no more recorded responses")
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
        return _is_retryable_provider_error(exc)

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
                if not _is_failoverable_provider_error(exc):
                    self._last_failures = failures
                    raise
                continue
        self._last_failures = failures
        joined = " | ".join(failures) if failures else "no providers tried"
        raise AgintorError(f"All providers failed: {joined}")


def _copy_json_like(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_json_like(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json_like(item) for item in value]
    return value


def provider_payload(provider: ModelProvider) -> dict[str, Any]:
    if isinstance(provider, RetryProvider):
        return {
            "kind": "retry",
            "max_retries": provider.max_retries,
            "backoff_s": provider.backoff_s,
            "max_backoff_s": provider.max_backoff_s,
            "wrapped": provider_payload(provider.wrapped),
        }
    if isinstance(provider, FailoverProvider):
        return {
            "kind": "failover",
            "providers": [provider_payload(item) for item in provider.providers],
        }
    if isinstance(provider, ReplayProvider):
        return {
            "kind": "replay",
            "replay_file": provider.replay_file,
        }
    if isinstance(provider, OpenAIProvider):
        return {
            "kind": "openai",
            "api_key": getattr(provider, "api_key", None) if getattr(provider, "api_key_explicit", False) else None,
            "api_key_file": getattr(provider, "api_key_file", None) or None,
            "base_url": getattr(provider, "base_url", None),
            "base_url_env": getattr(provider, "base_url_env", None),
            "api_key_env": getattr(provider, "api_key_env", None),
            "api_key_file_env": getattr(provider, "api_key_file_env", None),
            "model_map": _copy_json_like(getattr(provider, "model_map", {})),
            "model_envs": _copy_json_like(getattr(provider, "model_envs", {})),
            "default_models": _copy_json_like(getattr(provider, "default_models", {})),
            "temperature": getattr(provider, "temperature", None),
            "pricing_map": _copy_json_like(getattr(provider, "pricing_map", {})),
            "pricing_env": getattr(provider, "pricing_env", None),
            "reasoning_effort_map": _copy_json_like(getattr(provider, "reasoning_effort_map", {})),
        }
    if isinstance(provider, MiniMaxProvider):
        return {
            "kind": "minimax",
            "api_key": getattr(provider, "api_key", None) if getattr(provider, "api_key_explicit", False) else None,
            "api_key_file": getattr(provider, "api_key_file", None) or None,
            "base_url": getattr(provider, "base_url", None),
            "base_url_env": getattr(provider, "base_url_env", None),
            "api_key_env": getattr(provider, "api_key_env", None),
            "api_key_file_env": getattr(provider, "api_key_file_env", None),
            "model_map": _copy_json_like(getattr(provider, "model_map", {})),
            "model_envs": _copy_json_like(getattr(provider, "model_envs", {})),
            "default_models": _copy_json_like(getattr(provider, "default_models", {})),
            "temperature": getattr(provider, "temperature", None),
            "pricing_map": _copy_json_like(getattr(provider, "pricing_map", {})),
            "pricing_env": getattr(provider, "pricing_env", None),
        }
    if isinstance(provider, LocalDeterministicProvider):
        return {"kind": "local"}
    raise ProviderConfigurationError(f"unsupported provider type for payload serialization: {provider.__class__.__name__}")


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


def provider_payload_file_paths(payload: Mapping[str, Any]) -> list[str]:
    paths: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in _PROVIDER_FILE_KEYS and value:
                    paths.add(str(value))
                    continue
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(payload)
    return sorted(paths)


def rewrite_provider_payload_file_paths(payload: Mapping[str, Any], path_map: Mapping[str, str]) -> dict[str, Any]:
    def rewrite(node: Any) -> Any:
        if isinstance(node, Mapping):
            rewritten: dict[str, Any] = {}
            for key, value in node.items():
                if key in _PROVIDER_FILE_KEYS and value:
                    rewritten[str(key)] = path_map.get(str(value), str(value))
                else:
                    rewritten[str(key)] = rewrite(value)
            return rewritten
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        return node

    return rewrite(payload)


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
        api_key_file_env = _PROVIDER_DEFAULTS.get(name, {}).get("api_key_file_env")
    return str(api_key_file_env) if api_key_file_env else None


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
        return OpenAIProvider(**provider_kwargs)
    if provider_name == "minimax":
        return MiniMaxProvider(**provider_kwargs)
    if provider_name == "replay":
        replay_file = provider_kwargs.get("replay_file")
        if not replay_file:
            raise ProviderConfigurationError("Replay provider requires replay_file=<path>")
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
    "build_provider_from_payload",
    "build_provider",
    "known_provider_environment_names",
    "load_api_key_from_file",
    "load_openai_api_key_from_file",
    "provider_payload",
    "provider_payload_file_paths",
    "provider_environment_names",
    "provider_environment_names_for_instance",
    "provider_api_key_file_env_name",
    "provider_profile_for_name",
    "rewrite_provider_payload_file_paths",
    "resolve_api_key",
    "resolve_openai_api_key",
]
