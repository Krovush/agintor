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

_RETRYABLE_ERROR_TOKENS = ("rate", "429", "timeout", "temporar", "connection", "503", "unavailable")


def _provider_error_text(exc: Exception) -> str:
    return str(exc).strip().lower()


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, ProviderExhaustedError):
        return False
    text = _provider_error_text(exc)
    return any(token in text for token in _RETRYABLE_ERROR_TOKENS)


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
