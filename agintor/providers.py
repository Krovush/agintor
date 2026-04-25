from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
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
from .schemas import ModelRequest, ModelResponse, ReplayAllocation


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
    "replay": {"replay_file", "rows", "cursor", "cursor_start", "cursor_end"},
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

    class ReplayAllocator:
        def __init__(self, rows: list[dict[str, Any]], *, cursor: int = 0) -> None:
            self.rows = [dict(row) for row in rows]
            self.cursor = max(0, min(int(cursor or 0), len(self.rows)))
            self.lock = Lock()

    class ReplayCoordinator:
        def __init__(
            self,
            allocator: "ReplayProvider.ReplayAllocator",
            *,
            replay_file: str | None = None,
            cursor: int | None = None,
            cursor_start: int | None = None,
            cursor_end: int | None = None,
            use_allocator_cursor: bool = False,
        ) -> None:
            self._allocator = allocator
            self.replay_file = str(replay_file) if replay_file else None
            row_count = len(self._allocator.rows)
            default_start = self._allocator.cursor if use_allocator_cursor else 0
            self._cursor_start = max(
                0,
                min(int(cursor_start if cursor_start is not None else default_start), row_count),
            )
            raw_end = row_count if cursor_end is None else int(cursor_end)
            self._cursor_end = max(self._cursor_start, min(raw_end, row_count))
            if use_allocator_cursor:
                self._cursor = self._allocator.cursor
            else:
                seed_cursor = self._cursor_start if cursor is None else int(cursor)
                self._cursor = max(self._cursor_start, min(seed_cursor, self._cursor_end))
            self._use_allocator_cursor = bool(use_allocator_cursor)
            self._lock = Lock()

        def next_row(self) -> tuple[int, dict[str, Any]]:
            if self._use_allocator_cursor:
                with self._allocator.lock:
                    if self._allocator.cursor >= len(self._allocator.rows):
                        raise ProviderExhaustedError("Replay provider exhausted: no more recorded responses")
                    record_index = self._allocator.cursor
                    row = dict(self._allocator.rows[record_index])
                    self._allocator.cursor += 1
                    self._cursor = self._allocator.cursor
                    return record_index, row
            with self._lock:
                if self._cursor >= self._cursor_end:
                    raise ProviderExhaustedError("Replay provider exhausted: no more reserved responses")
                record_index = self._cursor
                row = dict(self._allocator.rows[record_index])
                self._cursor += 1
                return record_index, row

        def reserve_window(self, *, row_count: int, allocation_key: str) -> ReplayAllocation:
            size = max(0, int(row_count or 0))
            with self._allocator.lock:
                start = int(self._allocator.cursor)
                end = start + size
                if end > len(self._allocator.rows):
                    raise ProviderExhaustedError(
                        f"Replay provider exhausted while reserving {size} rows for {allocation_key}"
                    )
                self._allocator.cursor = end
                if self._use_allocator_cursor:
                    self._cursor = self._allocator.cursor
            return ReplayAllocation(
                allocation_key=str(allocation_key),
                cursor_start=start,
                cursor_end=end,
                next_cursor=start,
            )

        def clone_shared(self) -> "ReplayProvider.ReplayCoordinator":
            return ReplayProvider.ReplayCoordinator(
                self._allocator,
                replay_file=self.replay_file,
                use_allocator_cursor=True,
            )

        def clone_window(self, allocation: ReplayAllocation) -> "ReplayProvider.ReplayCoordinator":
            return ReplayProvider.ReplayCoordinator(
                self._allocator,
                replay_file=self.replay_file,
                cursor=allocation.next_cursor,
                cursor_start=allocation.cursor_start,
                cursor_end=allocation.cursor_end,
                use_allocator_cursor=False,
            )

        def current_allocation(self, allocation_key: str | None = None) -> ReplayAllocation | None:
            if self._use_allocator_cursor:
                return None
            return ReplayAllocation(
                allocation_key=str(allocation_key or ""),
                cursor_start=self._cursor_start,
                cursor_end=self._cursor_end,
                next_cursor=self._cursor,
            )

        def can_apply_allocation(self, allocation: ReplayAllocation | None) -> bool:
            if allocation is None:
                return False
            return int(allocation.cursor_end) <= len(self._allocator.rows)

        def snapshot_payload(self) -> dict[str, Any]:
            payload = {"kind": "replay", "replay_file": self.replay_file}
            if self.replay_file is None:
                payload["rows"] = _copy_json_like(self._allocator.rows)
            cursor_value = self._allocator.cursor if self._use_allocator_cursor else self._cursor
            if cursor_value:
                payload["cursor"] = int(cursor_value)
            if not self._use_allocator_cursor:
                payload["cursor_start"] = int(self._cursor_start)
                payload["cursor_end"] = int(self._cursor_end)
            return payload

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        replay_file: str | None = None,
        coordinator: "ReplayProvider.ReplayCoordinator" | None = None,
        cursor: int = 0,
        cursor_start: int | None = None,
        cursor_end: int | None = None,
    ) -> None:
        super().__init__("replay")
        use_allocator_cursor = cursor_start is None and cursor_end is None
        self._coordinator = coordinator or self.ReplayCoordinator(
            self.ReplayAllocator(rows, cursor=cursor),
            replay_file=replay_file,
            cursor=cursor,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            use_allocator_cursor=use_allocator_cursor,
        )
        self.replay_file = self._coordinator.replay_file

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        cursor: int = 0,
        cursor_start: int | None = None,
        cursor_end: int | None = None,
    ) -> "ReplayProvider":
        replay_path = str(Path(path))
        try:
            payload = json.loads(Path(replay_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderConfigurationError(f"Replay provider file is not readable: {replay_path}") from exc
        if not isinstance(payload, list):
            raise ProviderConfigurationError("Replay provider file must contain a JSON array of response rows")
        if any(not isinstance(row, Mapping) for row in payload):
            raise ProviderConfigurationError("Replay provider rows must be JSON objects")
        return cls(
            [dict(row) for row in payload],
            replay_file=replay_path,
            cursor=cursor,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
        )

    def _spawn_clone(self, coordinator: "ReplayProvider.ReplayCoordinator") -> "ReplayProvider":
        return ReplayProvider([], replay_file=self.replay_file, coordinator=coordinator)

    def shared_clone(self) -> "ReplayProvider":
        return self._spawn_clone(self._coordinator.clone_shared())

    def reserve_rows(self, row_count: int, *, allocation_key: str) -> ReplayAllocation:
        return self._coordinator.reserve_window(row_count=row_count, allocation_key=allocation_key)

    def clone_for_allocation(self, allocation: ReplayAllocation) -> "ReplayProvider":
        return self._spawn_clone(self._coordinator.clone_window(allocation))

    def current_allocation(self) -> ReplayAllocation | None:
        current = self._coordinator.current_allocation()
        if current is None:
            return None
        allocation_key = current.allocation_key or ""
        return current.model_copy(update={"allocation_key": allocation_key}, deep=True)

    def can_apply_allocation(self, allocation: ReplayAllocation | None) -> bool:
        return self._coordinator.can_apply_allocation(allocation)

    def generate(self, request: ModelRequest) -> ModelResponse:
        record_index, row = self._coordinator.next_row()
        metadata = getattr(request, "metadata", {}) or {}
        response = ModelResponse(
            text=str(row.get("text", "")),
            raw={
                "provider": "replay",
                "record_index": record_index,
                "request_model_class": request.model_class,
                "trace_context": dict(metadata.get("trace_context", {})) if isinstance(metadata, Mapping) else {},
            },
            model_name=str(row.get("model_name", f"replay/{request.model_class}")),
            input_tokens=int(row.get("input_tokens", 0) or 0),
            output_tokens=int(row.get("output_tokens", 0) or 0),
            token_estimate=int(row.get("token_estimate", 0) or 0),
            latency_s=float(row.get("latency_s", 0.0) or 0.0),
            dollar_cost=float(row.get("dollar_cost", 0.0) or 0.0),
            trace_call_id=str(row.get("trace_call_id") or row.get("call_id") or "").strip() or None,
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
        return provider._coordinator.snapshot_payload()
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
    "clone_provider",
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
