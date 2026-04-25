from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Mapping

from .exceptions import AgintorError
from .runtime_profile import HostedProviderProfile
from .schemas import ModelRequest, ModelResponse
from .utils import count_tokens_rough


def _coerce_price(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def normalize_pricing_map(
    pricing_map: Mapping[str, Mapping[str, Any]] | None,
    *,
    pricing_env: str | None = None,
) -> dict[str, dict[str, float]]:
    source: Mapping[str, Mapping[str, Any]] = pricing_map or {}
    if not source and pricing_env and os.environ.get(pricing_env):
        try:
            source = json.loads(os.environ[pricing_env])
        except Exception:
            source = {}
    normalized: dict[str, dict[str, float]] = {}
    for model_name, row in source.items():
        if hasattr(row, "get"):
            input_price = _coerce_price(row.get("input_per_1m", row.get("input", 0.0)))
            output_price = _coerce_price(row.get("output_per_1m", row.get("output", 0.0)))
        else:
            input_price = _coerce_price(row)
            output_price = input_price
        normalized[str(model_name)] = {
            "input_per_1m": input_price,
            "output_per_1m": output_price,
        }
    return normalized


_HOSTED_KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9][A-Za-z0-9_-]{20,})")


def load_api_key_from_file(path: str | Path, *, provider_label: str = "Hosted provider") -> str:
    key_path = Path(path)
    if not key_path.exists():
        raise AgintorError(f"{provider_label} API key file does not exist: {key_path}")
    content = key_path.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    if not content:
        raise AgintorError(f"{provider_label} API key file is empty: {key_path}")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip().upper().endswith("API_KEY") or name.strip().upper() == "API_KEY":
                candidate = value.strip().strip("\"'")
                if candidate:
                    return candidate
        if line.startswith("sk-"):
            return line.strip().strip("\"'")
    match = _HOSTED_KEY_PATTERN.search(content)
    if match:
        return match.group(1)
    non_empty_lines = [line.strip().strip("\"'") for line in content.splitlines() if line.strip()]
    if len(non_empty_lines) == 1:
        return non_empty_lines[0]
    raise AgintorError(f"Unable to parse a {provider_label} API key from: {key_path}")


def load_openai_api_key_from_file(path: str | Path) -> str:
    return load_api_key_from_file(path, provider_label="OpenAI")


def resolve_api_key(
    api_key: str | None = None,
    api_key_file: str | Path | None = None,
    *,
    api_key_env: str | None = None,
    api_key_file_env: str | None = None,
    provider_label: str = "Hosted provider",
) -> str | None:
    if api_key:
        return api_key
    env_key = os.environ.get(api_key_env) if api_key_env else None
    if env_key:
        return env_key
    env_key_file = os.environ.get(api_key_file_env) if api_key_file_env else None
    target_path = api_key_file or env_key_file
    if target_path:
        return load_api_key_from_file(target_path, provider_label=provider_label)
    return None


def resolve_openai_api_key(api_key: str | None = None, api_key_file: str | Path | None = None) -> str | None:
    return resolve_api_key(
        api_key=api_key,
        api_key_file=api_key_file,
        api_key_env="OPENAI_API_KEY",
        api_key_file_env="AGINTOR_OPENAI_KEY_FILE",
        provider_label="OpenAI",
    )


def stringify_response_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def request_max_output_tokens(metadata: Mapping[str, Any]) -> int | None:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        parsed = optional_int(metadata.get(key))
        if parsed is not None:
            return parsed
    return None


def usage_token_value(usage: Any, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        parsed = optional_int(value)
        if parsed is not None:
            return parsed
    return 0


class ModelProvider(ABC):
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self._usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "dollar_cost": 0.0}

    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    def _record_usage(self, response: ModelResponse) -> None:
        self._usage["calls"] += 1
        self._usage["input_tokens"] += int(response.input_tokens)
        self._usage["output_tokens"] += int(response.output_tokens)
        self._usage["total_tokens"] += int(response.token_estimate)
        self._usage["dollar_cost"] += float(response.dollar_cost)

    def usage_summary(self) -> dict[str, Any]:
        return dict(self._usage)


def _deterministic_tool_expression(description: str, args: Mapping[str, Any]) -> str:
    desc = description.lower()
    arg_names = sorted(str(name) for name in args)
    if not arg_names:
        return "0"
    if "numbers" in args and "modulus" in args and any(token in desc for token in ("square", "squared", "mod")):
        return "sum(x*x for x in numbers) % modulus"
    if "numbers" in args and any(token in desc for token in ("sum", "total", "add")):
        return "sum(numbers)"
    if any(token in desc for token in ("range", "difference")) and len(arg_names) >= 2:
        joined = ", ".join(arg_names)
        return f"max({joined}) - min({joined})"
    if any(token in desc for token in ("add", "sum", "total", "plus")) and len(arg_names) >= 2:
        return " + ".join(arg_names)
    return arg_names[0]


def _deterministic_tool_spec_payload(prompt: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    prompt_payload: dict[str, Any] = {}
    try:
        loaded = json.loads(prompt)
        if isinstance(loaded, dict):
            prompt_payload = dict(loaded)
    except Exception:
        prompt_payload = {}
    description = str(prompt_payload.get("description", payload.get("description", "")))
    raw_args = prompt_payload.get("args", payload.get("args", {}))
    args = dict(raw_args) if isinstance(raw_args, Mapping) else dict(payload.get("args", {}))
    expression = str(payload.get("expression") or "").strip()
    if not expression or (expression == "0" and args):
        expression = _deterministic_tool_expression(description, args)
    return {
        "expression": expression,
        "description": description,
        "args": args,
    }


def _deterministic_repo_patch_payload(prompt: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    target_paths = [str(path).strip() for path in payload.get("target_file_paths", []) if str(path).strip()]
    snapshots: list[dict[str, Any]] = []
    marker = "Target files:"
    if marker in prompt:
        snapshot_text = prompt.rsplit(marker, 1)[1].strip()
        try:
            loaded = json.loads(snapshot_text)
            if isinstance(loaded, list):
                snapshots = [dict(item) for item in loaded if isinstance(item, Mapping)]
        except Exception:
            snapshots = []
    snapshot_by_path = {str(item.get("path", "")).strip(): item for item in snapshots if str(item.get("path", "")).strip()}
    files: list[dict[str, str]] = []
    for path in target_paths or sorted(snapshot_by_path):
        current = str(snapshot_by_path.get(path, {}).get("content", ""))
        suffix = "\n" if current and not current.endswith("\n") else ""
        files.append(
            {
                "path": path,
                "updated_content": f"{current}{suffix}Local deterministic repo_patch update.\n",
            }
        )
    return {
        "summary": "Applied deterministic local repo patch.",
        "files": files,
    }


def _prompt_excerpt(prompt: str, *, words: int) -> str:
    tokens = [token for token in str(prompt or "").split() if token]
    return " ".join(tokens[:words]).strip()


def _schema_string_sample(prompt: str, field_name: str | None) -> str:
    label = str(field_name or "response").strip().replace("_", " ")
    short_excerpt = _prompt_excerpt(prompt, words=6)
    full_excerpt = _prompt_excerpt(prompt, words=16)
    lowered = label.lower()
    if lowered == "title":
        return short_excerpt or "Title"
    if lowered == "summary":
        return full_excerpt or "Summary"
    return full_excerpt or label or "response"


def _schema_sample_value(schema: Mapping[str, Any], prompt: str, field_name: str | None = None) -> Any:
    default = schema.get("default")
    if default is not None:
        return default
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]
    schema_type = schema.get("type")
    if schema_type == "object" or (schema_type is None and isinstance(schema.get("properties"), Mapping)):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return {}
        return {
            str(name): _schema_sample_value(
                definition if isinstance(definition, Mapping) else {},
                prompt,
                str(name),
            )
            for name, definition in properties.items()
        }
    if schema_type == "array":
        items = schema.get("items", {})
        if isinstance(items, Mapping):
            return [_schema_sample_value(items, prompt, field_name)]
        return []
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    return _schema_string_sample(prompt, field_name)


class LocalDeterministicProvider(ModelProvider):
    def __init__(self) -> None:
        super().__init__("local")

    def generate(self, request: ModelRequest) -> ModelResponse:
        start = time.perf_counter()
        mode = request.metadata.get("mode", "text")
        if mode == "summary":
            lines = [line.strip() for line in request.prompt.splitlines() if line.strip()]
            text = " | ".join(lines[:4])
        elif mode == "child_instruction":
            payload = request.metadata.get("payload", {})
            text = f"Solve subgoal {payload.get('op_id', 'unknown')} with output key {payload.get('output_key', 'value')}"
        elif mode == "tool_spec":
            payload = request.metadata.get("payload", {})
            text = json.dumps(_deterministic_tool_spec_payload(request.prompt, payload), sort_keys=True)
        elif mode == "repo_patch":
            payload = request.metadata.get("payload", {})
            text = json.dumps(_deterministic_repo_patch_payload(request.prompt, payload), sort_keys=True)
        elif mode == "user_request":
            payload = request.metadata.get("payload", {})
            output_schema = payload.get("output_schema", {})
            if isinstance(output_schema, Mapping) and output_schema:
                text = json.dumps(
                    _schema_sample_value(output_schema, request.prompt),
                    sort_keys=True,
                )
            else:
                text = f"Best effort response: {request.prompt.strip()}"
        else:
            text = request.prompt.strip()
        latency = time.perf_counter() - start
        input_tokens = count_tokens_rough(request.instructions + request.prompt)
        output_tokens = count_tokens_rough(text)
        response = ModelResponse(
            text=text,
            raw={"provider": "local"},
            model_name=f"local/{request.model_class}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_estimate=input_tokens + output_tokens,
            latency_s=latency,
            dollar_cost=0.0,
        )
        self._record_usage(response)
        return response


class HostedProviderBase(ModelProvider):
    def __init__(
        self,
        *,
        provider_name: str,
        provider_defaults: Mapping[str, Any],
        api_key: str | None = None,
        api_key_file: str | Path | None = None,
        base_url: str | None = None,
        base_url_env: str | None = None,
        api_key_env: str | None = None,
        api_key_file_env: str | None = None,
        model_map: Mapping[str, str] | None = None,
        model_envs: Mapping[str, str] | None = None,
        default_models: Mapping[str, str] | None = None,
        temperature: float | None = None,
        pricing_map: Mapping[str, Mapping[str, Any]] | None = None,
        pricing_env: str | None = None,
    ) -> None:
        super().__init__(provider_name)
        defaults = dict(provider_defaults)
        self.base_url_env = base_url_env or defaults.get("base_url_env")
        self.api_key_env = api_key_env or defaults.get("api_key_env")
        self.api_key_file_env = api_key_file_env or defaults.get("api_key_file_env")
        self.model_envs = {**dict(defaults.get("model_envs", {})), **dict(model_envs or {})}
        self.default_models = {**dict(defaults.get("default_models", {})), **dict(default_models or {})}
        self.pricing_env = pricing_env or defaults.get("pricing_env")
        self.api_key_explicit = api_key not in (None, "")
        self.api_key_file = str(api_key_file) if api_key_file is not None else os.environ.get(self.api_key_file_env or "")
        self.api_key = resolve_api_key(
            api_key=api_key,
            api_key_file=self.api_key_file,
            api_key_env=self.api_key_env,
            api_key_file_env=self.api_key_file_env,
            provider_label=provider_name.capitalize(),
        )
        self.base_url = base_url or (os.environ.get(self.base_url_env) if self.base_url_env else None) or defaults.get("base_url")
        self.model_map = dict(model_map or {})
        self.temperature = temperature
        self.pricing_map = normalize_pricing_map(pricing_map, pricing_env=self.pricing_env)
        self._client = None

    def _credential_hints(self) -> list[str]:
        hints: list[str] = []
        if self.api_key_env:
            hints.append(str(self.api_key_env))
        if self.api_key_file_env:
            hints.append(str(self.api_key_file_env))
        return hints

    def _ensure_credentials_available(self) -> None:
        if self.api_key:
            return
        expected = self._credential_hints()
        provider_label = str(self.provider_name or "hosted provider").strip()
        if expected:
            raise AgintorError(
                f"{provider_label} credentials are required for hosted model calls. "
                f"Provide one of: {', '.join(expected)}."
            )
        raise AgintorError(f"{provider_label} credentials are required for hosted model calls.")

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        self._ensure_credentials_available()
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - exercised only in live environments
            raise AgintorError("The official openai package is not installed. Install with `pip install .[hosted]`.") from exc
        kwargs: Dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = OpenAI(**kwargs)
        return self._client

    def resolve_model(self, model_class: str) -> str:
        if model_class in self.model_map:
            return self.model_map[model_class]
        if model_class in self.model_envs:
            return os.environ.get(self.model_envs[model_class], self.default_models.get(model_class, model_class))
        if model_class in self.default_models:
            return self.default_models[model_class]
        return model_class

    def _estimate_cost(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        pricing = self.pricing_map.get(model_name)
        if not pricing:
            return 0.0
        return (input_tokens / 1_000_000.0) * pricing["input_per_1m"] + (output_tokens / 1_000_000.0) * pricing["output_per_1m"]


def provider_kwargs_from_profile(provider_profile: HostedProviderProfile | None) -> dict[str, Any]:
    if provider_profile is None:
        return {}
    return {
        "base_url": provider_profile.base_url,
        "base_url_env": provider_profile.base_url_env,
        "api_key_env": provider_profile.api_key_env,
        "api_key_file_env": provider_profile.api_key_file_env,
        "model_map": dict(provider_profile.model_map),
        "reasoning_effort_map": dict(provider_profile.reasoning_effort_map),
        "temperature": provider_profile.temperature,
        "pricing_map": dict(provider_profile.pricing_map),
        "pricing_env": provider_profile.pricing_env,
    }
