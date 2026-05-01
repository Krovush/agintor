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

from .retry import (
    _RETRYABLE_ERROR_TOKENS,
    _provider_error_text,
)

_FAILOVER_ERROR_TOKENS = _RETRYABLE_ERROR_TOKENS + ("overloaded", "bad gateway", "gateway timeout")


def _is_failoverable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, ProviderExhaustedError):
        return False
    text = _provider_error_text(exc)
    return any(token in text for token in _FAILOVER_ERROR_TOKENS)


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
