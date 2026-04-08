from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Mapping

from .openai_trace import persist_openai_trace
from .provider_common import (
    HostedProviderBase,
    count_tokens_rough,
    request_max_output_tokens,
    stringify_response_input,
    usage_token_value,
)
from .schemas import ModelRequest, ModelResponse


OPENAI_PROVIDER_DEFAULTS: dict[str, Any] = {
    "base_url": None,
    "base_url_env": "OPENAI_BASE_URL",
    "api_key_env": "OPENAI_API_KEY",
    "api_key_file_env": "AGINTOR_OPENAI_KEY_FILE",
    "default_models": {
        "small": "gpt-5.4-mini",
        "medium": "gpt-5.4-mini",
        "large": "gpt-5.4-mini",
    },
    "model_envs": {
        "small": "AGINTOR_OPENAI_SMALL_MODEL",
        "medium": "AGINTOR_OPENAI_MEDIUM_MODEL",
        "large": "AGINTOR_OPENAI_LARGE_MODEL",
    },
    "pricing_env": "AGINTOR_OPENAI_PRICING",
}

OPENAI_REASONING_DEFAULTS = {
    "small": "high",
    "medium": "high",
    "large": "high",
}


def _normalize_reasoning_effort(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized == "non":
        return "none"
    return normalized


class OpenAIProvider(HostedProviderBase):
    def __init__(
        self,
        *,
        reasoning_effort_map: Mapping[str, str] | None = None,
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
        super().__init__(
            provider_name="openai",
            provider_defaults=OPENAI_PROVIDER_DEFAULTS,
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
        merged_reasoning = {**OPENAI_REASONING_DEFAULTS, **dict(reasoning_effort_map or {})}
        self.reasoning_effort_map = {
            str(key): normalized
            for key, value in merged_reasoning.items()
            if (normalized := _normalize_reasoning_effort(value)) is not None
        }

    def resolve_reasoning_effort(self, model_class: str, model_name: str | None = None) -> str | None:
        if model_class in self.reasoning_effort_map:
            return self.reasoning_effort_map[model_class]
        resolved_model = model_name or self.resolve_model(model_class)
        if resolved_model in self.reasoning_effort_map:
            return self.reasoning_effort_map[resolved_model]
        return None

    def _response_to_model_response(
        self,
        *,
        response: Any,
        model_name: str,
        prompt_text: str,
        reasoning_effort: str | None,
    ) -> ModelResponse:
        output_text = getattr(response, "output_text", None)
        if output_text is None:
            output_text = str(response)
        usage = getattr(response, "usage", None)
        input_tokens = usage_token_value(usage, "input_tokens")
        if not input_tokens:
            input_tokens = count_tokens_rough(prompt_text)
        output_tokens = usage_token_value(usage, "output_tokens")
        if not output_tokens and output_text != "":
            output_tokens = count_tokens_rough(output_text)
        total_tokens = usage_token_value(usage, "total_tokens")
        if not total_tokens:
            total_tokens = input_tokens + output_tokens
        return ModelResponse(
            text=output_text,
            raw={
                "provider": self.provider_name,
                "model": model_name,
                "response_id": getattr(response, "id", None),
                "status": getattr(response, "status", None),
                "reasoning_effort": reasoning_effort,
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
        metadata: Mapping[str, Any] | None = None,
        record_usage: bool = True,
        **kwargs: Any,
    ) -> tuple[Any, ModelResponse]:
        client = self._client_or_raise()
        model_name = self.resolve_model(model_class)
        reasoning_effort = self.resolve_reasoning_effort(model_class, model_name)
        start = time.perf_counter()
        payload: Dict[str, Any] = {"model": model_name, "input": input}
        if instructions:
            payload["instructions"] = instructions
        if self.temperature is not None and "temperature" not in kwargs:
            payload["temperature"] = self.temperature
        if reasoning_effort and "reasoning" not in kwargs:
            payload["reasoning"] = {"effort": reasoning_effort}
        max_output_tokens = request_max_output_tokens(metadata or {})
        if max_output_tokens is not None and "max_output_tokens" not in kwargs:
            payload["max_output_tokens"] = max_output_tokens
        payload.update(kwargs)
        method = getattr(client.responses, method_name)
        try:
            response = method(**payload)  # pragma: no cover - live path only
        except Exception as exc:
            persist_openai_trace(
                provider=self.provider_name,
                method_name=method_name,
                model_class=model_class,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                instructions=instructions,
                input_value=input,
                request_payload=payload,
                request_metadata=metadata,
                response=None,
                response_text="",
                input_tokens=count_tokens_rough(f"{instructions}\n{stringify_response_input(input)}"),
                output_tokens=0,
                total_tokens=0,
                latency_s=time.perf_counter() - start,
                error=str(exc),
            )
            raise
        recorded = self._response_to_model_response(
            response=response,
            model_name=model_name,
            prompt_text=f"{instructions}\n{stringify_response_input(input)}",
            reasoning_effort=reasoning_effort,
        )
        recorded.latency_s = time.perf_counter() - start
        persist_openai_trace(
            provider=self.provider_name,
            method_name=method_name,
            model_class=model_class,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            instructions=instructions,
            input_value=input,
            request_payload=payload,
            request_metadata=metadata,
            response=response,
            response_text=recorded.text,
            input_tokens=recorded.input_tokens,
            output_tokens=recorded.output_tokens,
            total_tokens=recorded.token_estimate,
            latency_s=recorded.latency_s,
            error=None,
        )
        if record_usage:
            self._record_usage(recorded)
        return response, recorded

    def create_response(self, *, model_class: str, instructions: str = "", input: Any = "", metadata: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        response, _ = self._dispatch_response(
            method_name="create",
            model_class=model_class,
            instructions=instructions,
            input=input,
            metadata=metadata,
            record_usage=True,
            **kwargs,
        )
        return response

    def parse_response(self, *, text_format: type[Any], model_class: str, instructions: str = "", input: Any = "", metadata: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        response, _ = self._dispatch_response(
            method_name="parse",
            model_class=model_class,
            instructions=instructions,
            input=input,
            metadata=metadata,
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
            metadata=request.metadata,
            record_usage=False,
        )
        self._record_usage(result)
        return result
