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

from .failover import FailoverProvider
from .retry import RetryProvider

_PROVIDER_FILE_KEYS = {"api_key_file", "replay_file"}


def _copy_json_like(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_json_like(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json_like(item) for item in value]
    return value


def provider_payload(provider: ModelProvider) -> dict[str, Any]:
    from .replay import ReplayProvider

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
    provider_name = str(getattr(provider, "provider_name", "") or "").strip().lower()
    if provider_name == "openai":
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
    if provider_name == "minimax":
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
