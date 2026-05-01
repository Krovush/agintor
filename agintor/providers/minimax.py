from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Mapping

from ..core.exceptions import AgintorError
from ..tracing import persist_openai_trace
from .base import (
    HostedProviderBase,
    count_tokens_rough,
    request_max_output_tokens,
    stringify_response_input,
)
from ..contracts import ModelRequest, ModelResponse


MINIMAX_PROVIDER_DEFAULTS: dict[str, Any] = {
    "base_url": "https://api.minimax.io/anthropic",
    "base_url_env": "AGINTOR_MAS_MINIMAX_BASE_URL",
    "api_key_env": "AGINTOR_MAS_MINIMAX_API_KEY",
    "api_key_file_env": "AGINTOR_MAS_MINIMAX_KEY_FILE",
    "default_models": {
        "small": "MiniMax-M2.7-Flash",
        "medium": "MiniMax-M2.7-Flash",
        "large": "MiniMax-M2.7-Flash",
    },
    "model_envs": {
        "small": "AGINTOR_MAS_MINIMAX_SMALL_MODEL",
        "medium": "AGINTOR_MAS_MINIMAX_MEDIUM_MODEL",
        "large": "AGINTOR_MAS_MINIMAX_LARGE_MODEL",
    },
    "pricing_env": "AGINTOR_MAS_MINIMAX_PRICING",
}

DEFAULT_MAX_OUTPUT_TOKENS = 8192


class MiniMaxProvider(HostedProviderBase):
    def __init__(
        self,
        *,
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
        **_: Any,
    ) -> None:
        super().__init__(
            provider_name="minimax",
            provider_defaults=MINIMAX_PROVIDER_DEFAULTS,
            api_key=api_key,
            api_key_file=api_key_file,
            base_url=base_url,
            base_url_env=base_url_env,
            api_key_env=api_key_env,
            api_key_file_env=api_key_file_env,
            model_map=model_map,
            model_envs=model_envs,
            default_models=default_models,
            temperature=temperature,
            pricing_map=pricing_map,
            pricing_env=pricing_env,
        )

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        self._ensure_credentials_available()
        try:
            import anthropic
        except Exception as exc:  # pragma: no cover - exercised only in live environments
            raise AgintorError("The official anthropic package is not installed. Install with `pip install .[hosted]`.") from exc
        kwargs: Dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _messages(self, prompt: str) -> list[dict[str, object]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    }
                ],
            }
        ]

    def _completion_to_model_response(
        self,
        *,
        response: Any,
        model_name: str,
        prompt_text: str,
    ) -> ModelResponse:
        output_text = ""
        for block in list(getattr(response, "content", None) or []):
            if getattr(block, "type", None) == "text":
                output_text += str(getattr(block, "text", ""))
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or count_tokens_rough(prompt_text))
        if not input_tokens:
            input_tokens = count_tokens_rough(prompt_text)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if not output_tokens and output_text != "":
            output_tokens = count_tokens_rough(output_text)
        total_tokens = input_tokens + output_tokens
        return ModelResponse(
            text=output_text,
            raw={
                "provider": self.provider_name,
                "model": model_name,
                "response_id": getattr(response, "id", None),
                "stop_reason": getattr(response, "stop_reason", None),
            },
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_estimate=total_tokens,
            latency_s=0.0,
            dollar_cost=self._estimate_cost(model_name, input_tokens, output_tokens),
        )

    def create_response(self, *, model_class: str, instructions: str = "", input: str = "", metadata: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        client = self._client_or_raise()
        model_name = self.resolve_model(model_class)
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": self._messages(input),
        }
        if instructions.strip() and "system" not in kwargs:
            payload["system"] = instructions
        if self.temperature is not None and "temperature" not in kwargs:
            payload["temperature"] = self.temperature
        max_output_tokens = request_max_output_tokens(metadata or {})
        if "max_tokens" not in kwargs:
            payload["max_tokens"] = max_output_tokens if max_output_tokens is not None else DEFAULT_MAX_OUTPUT_TOKENS
        payload.update(kwargs)
        return client.messages.create(**payload)  # pragma: no cover - live path only

    def _trace_request_payload(
        self,
        *,
        model_name: str,
        instructions: str,
        input: str,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": self._messages(input),
        }
        if instructions.strip():
            payload["system"] = instructions
        max_output_tokens = request_max_output_tokens(metadata or {})
        payload["max_tokens"] = max_output_tokens if max_output_tokens is not None else DEFAULT_MAX_OUTPUT_TOKENS
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def generate(self, request: ModelRequest) -> ModelResponse:
        model_name = self.resolve_model(request.model_class)
        start = time.perf_counter()
        trace_payload = self._trace_request_payload(
            model_name=model_name,
            instructions=request.instructions,
            input=request.prompt,
            metadata=request.metadata,
        )
        try:
            response = self.create_response(
                model_class=request.model_class,
                instructions=request.instructions,
                input=request.prompt,
                metadata=request.metadata,
            )
        except Exception as exc:
            persist_openai_trace(
                provider=self.provider_name,
                method_name="messages.create",
                model_class=request.model_class,
                model_name=model_name,
                reasoning_effort=None,
                instructions=request.instructions,
                input_value=request.prompt,
                request_payload=trace_payload,
                request_metadata=request.metadata,
                response=None,
                response_text="",
                input_tokens=count_tokens_rough(f"{request.instructions}\n{stringify_response_input(request.prompt)}"),
                output_tokens=0,
                total_tokens=0,
                latency_s=time.perf_counter() - start,
                error=str(exc),
            )
            raise
        recorded = self._completion_to_model_response(
            response=response,
            model_name=model_name,
            prompt_text=f"{request.instructions}\n{request.prompt}",
        )
        recorded.latency_s = time.perf_counter() - start
        trace_call_id = persist_openai_trace(
            provider=self.provider_name,
            method_name="messages.create",
            model_class=request.model_class,
            model_name=model_name,
            reasoning_effort=None,
            instructions=request.instructions,
            input_value=request.prompt,
            request_payload=trace_payload,
            request_metadata=request.metadata,
            response=response,
            response_text=recorded.text,
            input_tokens=recorded.input_tokens,
            output_tokens=recorded.output_tokens,
            total_tokens=recorded.token_estimate,
            latency_s=recorded.latency_s,
            error=None,
        )
        recorded.trace_call_id = trace_call_id
        if trace_call_id:
            recorded.raw["trace_call_id"] = trace_call_id
        self._record_usage(recorded)
        return recorded
