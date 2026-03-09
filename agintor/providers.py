from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Mapping

from .exceptions import AgintorError
from .schemas import ModelRequest, ModelResponse
from .utils import count_tokens_rough


def _coerce_price(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _normalize_pricing_map(pricing_map: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, float]]:
    source: Mapping[str, Mapping[str, Any]] = pricing_map or {}
    if not source and os.environ.get("AGINTOR_OPENAI_PRICING"):
        try:
            source = json.loads(os.environ["AGINTOR_OPENAI_PRICING"])
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


_OPENAI_KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9][A-Za-z0-9_-]{20,})")


def load_openai_api_key_from_file(path: str | Path) -> str:
    key_path = Path(path)
    if not key_path.exists():
        raise AgintorError(f"OpenAI API key file does not exist: {key_path}")
    content = key_path.read_text(encoding="utf-8").strip()
    if not content:
        raise AgintorError(f"OpenAI API key file is empty: {key_path}")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip().upper() in {"OPENAI_API_KEY", "API_KEY"}:
                candidate = value.strip().strip("\"'")
                if candidate:
                    return candidate
        if line.startswith("sk-"):
            return line.strip().strip("\"'")
    match = _OPENAI_KEY_PATTERN.search(content)
    if match:
        return match.group(1)
    non_empty_lines = [line.strip().strip("\"'") for line in content.splitlines() if line.strip()]
    if len(non_empty_lines) == 1:
        return non_empty_lines[0]
    raise AgintorError(f"Unable to parse an OpenAI API key from: {key_path}")


def resolve_openai_api_key(api_key: str | None = None, api_key_file: str | Path | None = None) -> str | None:
    if api_key:
        return api_key
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key
    env_key_file = os.environ.get("AGINTOR_OPENAI_KEY_FILE")
    target_path = api_key_file or env_key_file
    if target_path:
        return load_openai_api_key_from_file(target_path)
    return None


def _stringify_response_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


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


class ModelProvider(ABC):
    def __init__(self) -> None:
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


class LocalDeterministicProvider(ModelProvider):
    def __init__(self) -> None:
        super().__init__()

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


class OpenAIProvider(ModelProvider):
    def __init__(
        self,
        api_key: str | None = None,
        api_key_file: str | Path | None = None,
        base_url: str | None = None,
        model_map: Mapping[str, str] | None = None,
        temperature: float | None = None,
        pricing_map: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.api_key_file = str(api_key_file) if api_key_file is not None else os.environ.get("AGINTOR_OPENAI_KEY_FILE")
        self.api_key = resolve_openai_api_key(api_key=api_key, api_key_file=self.api_key_file)
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self.model_map = dict(model_map or {})
        self.temperature = temperature
        self.pricing_map = _normalize_pricing_map(pricing_map)
        self._client = None

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - exercised only in live environments
            raise AgintorError("The official openai package is not installed. Install with `pip install .[openai]`.") from exc
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
        if model_class == "small":
            return os.environ.get("AGINTOR_OPENAI_SMALL_MODEL", "gpt-5-mini")
        if model_class == "medium":
            return os.environ.get("AGINTOR_OPENAI_MEDIUM_MODEL", "gpt-5")
        if model_class == "large":
            return os.environ.get("AGINTOR_OPENAI_LARGE_MODEL", "gpt-5")
        return model_class

    def _estimate_cost(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        pricing = self.pricing_map.get(model_name)
        if not pricing:
            return 0.0
        return (input_tokens / 1_000_000.0) * pricing["input_per_1m"] + (output_tokens / 1_000_000.0) * pricing["output_per_1m"]

    def _response_to_model_response(
        self,
        *,
        response: Any,
        model_name: str,
        prompt_text: str,
        provider_name: str = "openai",
    ) -> ModelResponse:
        output_text = getattr(response, "output_text", None)
        if output_text is None:
            output_text = str(response)
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or count_tokens_rough(prompt_text))
        output_tokens = int(getattr(usage, "output_tokens", 0) or count_tokens_rough(output_text))
        total_tokens = int(getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens))
        return ModelResponse(
            text=output_text,
            raw={
                "provider": provider_name,
                "model": model_name,
                "response_id": getattr(response, "id", None),
                "status": getattr(response, "status", None),
            },
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_estimate=total_tokens,
            latency_s=0.0,
            dollar_cost=self._estimate_cost(model_name, input_tokens, output_tokens),
        )

    def _dispatch_response(
        self,
        *,
        method_name: str,
        model_class: str,
        instructions: str = "",
        input: Any = "",
        record_usage: bool = True,
        **kwargs: Any,
    ) -> tuple[Any, ModelResponse]:
        client = self._client_or_raise()
        model_name = self.resolve_model(model_class)
        start = time.perf_counter()
        payload: Dict[str, Any] = {"model": model_name, "input": input}
        if instructions:
            payload["instructions"] = instructions
        if self.temperature is not None and "temperature" not in kwargs:
            payload["temperature"] = self.temperature
        payload.update(kwargs)
        method = getattr(client.responses, method_name)
        response = method(**payload)  # pragma: no cover - live path only
        recorded = self._response_to_model_response(
            response=response,
            model_name=model_name,
            prompt_text=f"{instructions}\n{_stringify_response_input(input)}",
        )
        recorded.latency_s = time.perf_counter() - start
        if record_usage:
            self._record_usage(recorded)
        return response, recorded

    def create_response(self, *, model_class: str, instructions: str = "", input: Any = "", **kwargs: Any) -> Any:
        response, _ = self._dispatch_response(
            method_name="create",
            model_class=model_class,
            instructions=instructions,
            input=input,
            record_usage=True,
            **kwargs,
        )
        return response

    def parse_response(self, *, text_format: type[Any], model_class: str, instructions: str = "", input: Any = "", **kwargs: Any) -> Any:
        response, _ = self._dispatch_response(
            method_name="parse",
            model_class=model_class,
            instructions=instructions,
            input=input,
            text_format=text_format,
            record_usage=True,
            **kwargs,
        )
        return response

    def generate(self, request: ModelRequest) -> ModelResponse:
        _, result = self._dispatch_response(
            method_name="create",
            model_class=request.model_class,
            instructions=request.instructions,
            input=request.prompt,
            record_usage=False,
        )
        self._record_usage(result)
        return result


def build_provider(name: str = "local", **kwargs: Any) -> ModelProvider:
    if name == "local":
        return LocalDeterministicProvider()
    if name == "openai":
        return OpenAIProvider(**kwargs)
    raise ValueError(f"unknown provider {name}")
