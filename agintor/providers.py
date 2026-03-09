from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping

from .exceptions import AgintorError
from .schemas import ModelRequest, ModelResponse
from .utils import count_tokens_rough


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError


class LocalDeterministicProvider(ModelProvider):
    """Deterministic offline provider used by tests and default demo runtime.

    It is intentionally simple: the runtime is benchmark-driven and exact-verifier-driven,
    so this provider only needs to support summarization, child instruction drafting,
    and fallback text synthesis in a deterministic way.
    """

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
            text = json.dumps(payload, sort_keys=True)
        else:
            text = request.prompt.strip()
        latency = time.perf_counter() - start
        return ModelResponse(text=text, raw={"provider": "local"}, token_estimate=count_tokens_rough(request.instructions + request.prompt), latency_s=latency)


class OpenAIProvider(ModelProvider):
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model_map: Mapping[str, str] | None = None, temperature: float | None = None) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self.model_map = dict(model_map or {})
        self.temperature = temperature
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

    def generate(self, request: ModelRequest) -> ModelResponse:
        client = self._client_or_raise()
        model_name = self.resolve_model(request.model_class)
        start = time.perf_counter()
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "instructions": request.instructions,
            "input": request.prompt,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        response = client.responses.create(**kwargs)  # pragma: no cover - live path only
        latency = time.perf_counter() - start
        output_text = getattr(response, "output_text", None)
        if output_text is None:
            output_text = str(response)
        return ModelResponse(
            text=output_text,
            raw={"provider": "openai", "model": model_name},
            token_estimate=count_tokens_rough(request.instructions + request.prompt + output_text),
            latency_s=latency,
        )



def build_provider(name: str = "local", **kwargs: Any) -> ModelProvider:
    if name == "local":
        return LocalDeterministicProvider()
    if name == "openai":
        return OpenAIProvider(**kwargs)
    raise ValueError(f"unknown provider {name}")
