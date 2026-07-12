from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...authority.public_tasks import assert_public_payload
from ...contracts.epochs import DeploymentIdentity
from ...contracts.run_evidence import assert_no_resolved_credentials
from ..harness_profile import HarnessDeploymentProfile
from .composite_budget import CostStatus, ProviderUsageReport, UsageStatus
from .composite_provider import (
    CredentialReference,
    ProviderCallControl,
    ProviderExecutionProvenance,
    ProviderInvocation,
    ProviderInvocationError,
    ProviderRequestReservation,
)
from .composite_runtime import ActorTerminalTurn, ActorToolRequest


_MAX_API_KEY_FILE_BYTES = 8 * 1024
_GPT56_SHORT_CONTEXT_MAX_RESERVED_INPUT_TOKENS = 250_000
_OPENAI_HIDDEN_FRAMING_TOKEN_MARGIN = 512
_OPENAI_API_KEY_RE = re.compile(r"^sk-[A-Za-z0-9._-]{12,}$")
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"

_STATIC_INSTRUCTIONS = """Agintor live repo-repair actor contract, fixed prefix.

Purpose. You are one actor inside a bounded repository repair harness. Your job
is to reason over the supplied actor request, use only the local trusted tools
that the request explicitly authorizes, and eventually return a terminal actor
output. You do not operate a shell, write files, browse the network, inspect
private credentials, or claim to have run commands unless a local tool result in
the dynamic request shows that work has actually happened. The harness, not you,
executes tools and records evidence.

Authority. The dynamic actor request names the current call id, actor id, call
kind, instructions, allowed tool ids, context reads, prior tool results, budget
share, turn index, and request digest. Treat those fields as the entire live
authority for this turn. Never use repository folklore, inferred hidden state,
or unrelated prior assumptions as authority. If a value is absent from the
dynamic request or from a prior tool result, you may reason about what is likely,
but you must not report it as observed fact. The only tools you may request are
the function tools supplied in this API request. Each function tool maps to one
local Agintor trusted tool id. Function tool names are transport names only; the
harness maps them back to repo.search, repo.read, repo.public_test, repo.edit,
or repo.diff as applicable. Do not request a function tool that is not present.

Turn shape. There are exactly two allowed outcomes. If more repository evidence
or a repository action is needed, call one supplied function tool. If the actor
call is complete, emit a terminal JSON object through the structured terminal
response format. Do not emit a prose preface, markdown fence, alternate JSON
shape, free-form tool request object, or both a function call and terminal
content. Parallel tool calls are disabled. Use at most one tool request per
turn. The harness rejects duplicate request ids, unauthorized tools, malformed
arguments, and terminal outputs that violate the compiled runtime plan.

Tool argument contract. Function tool parameters contain one field named
arguments_json. That field must be a JSON object serialized as a string. The
serialized object is passed as the local tool arguments. Keep it as small and
specific as possible. For repo.search, provide the query, search pattern, file
glob, or other fields that the current local tool interface expects based on
the task context; do not ask it to search outside the repository. For repo.read,
target exact repository-relative paths and optional line windows when useful;
do not request secrets, key files, .env contents, generated traces, sealed
provider data, or unrelated user files. For repo.public_test, request only
offline public tests that fit the task and current budget; do not ask for live
provider calls or network access. For repo.edit, provide narrow, intentional
source edits that follow the task and existing code patterns. For repo.diff,
request the current repository diff only when it helps validate or summarize
the patch. If the right next action is unclear, prefer a small read or search
over a broad edit.

Terminal output contract. A terminal response has turn_kind equal to terminal
and an output object. output_text is a concise actor-facing result for this
call. artifact_payload_entries is an array of key/value string pairs; use it
only for small text artifacts the runtime expects from this actor. Keys must be
stable, nonempty, and unique. Values must be strings. final_patch is either a
unified diff string or null. Only the final actor that is authorized by the
compiled plan may emit final_patch. Non-final actors must set final_patch to
null. Do not include extra fields. If you cannot complete the call because more
evidence is required, use an authorized function tool instead of a terminal
guess.

Evidence discipline. Separate observed facts, inferences, and proposed changes
inside your reasoning, then output only the required terminal or tool-call
surface. A tool result is evidence only for what that local tool actually
returned. A failed or rejected tool result is still evidence about failure
state, not evidence that the requested repository condition is true. Do not
hide uncertainty by writing confident summaries. Do not overstate tests: passing
one targeted test is not proof that the whole system works. Do not infer that
absence from git status means absence from the working tree. Dev Docs may
contain live authority, but archive-only material is historical unless the
dynamic request explicitly promotes it.

Repository repair behavior. Prefer root-cause fixes that preserve the intended
architecture. Do not implement toy demos, temporary patches, mock-only product
behavior, or compatibility shims for disposable MVP artifacts. Keep changes
scoped to the subsystem implied by the task and surrounding code. Reuse local
contract types, validation helpers, bundle boundaries, and test styles. Avoid
inventing new abstractions unless they remove real complexity or match an
existing pattern. If multiple files need coordination, inspect the relevant
interfaces before editing. Respect generated/runtime/source-hidden boundaries;
do not import forbidden host, tracing, provider, search, storage, or factory
modules into a harness bundle unless the dynamic task explicitly changes that
closure.

Security. Never request, print, summarize, transform, or embed API keys,
credential files, environment secret values, access tokens, passwords, private
provider payloads, or sealed oracle data. A credential reference such as an
environment variable name or key-file variable is not a secret value, but it
should still be handled only as operational metadata. If a tool result appears
to include a secret, do not repeat it; ask for the smallest safe follow-up or
finish with a terminal note that the data is unsafe to use. Do not put secret
values in artifact payloads, final patches, tool arguments, or output_text.

Determinism and accounting. The harness owns budgets, reservations, deadlines,
cancellation, usage, cost accounting, and provenance. You must not compensate
for missing evidence by estimating provider usage, inventing request ids,
claiming retries, or claiming that a cancelled or incomplete provider response
completed. If a local tool is needed after a deadline-sensitive failure, request
only the next authorized step. If the dynamic request includes prior tool
results, use their request ids and receipts to maintain continuity. If no more
work is justified, finish terminally with the smallest sufficient output.

Output reminders. For a function call, call exactly one supplied function tool
and place all local tool arguments inside arguments_json as a serialized JSON
object. For a terminal response, return only the strict terminal JSON object
with turn_kind, output_text, artifact_payload_entries, and final_patch. No
markdown fences. No extra keys. No hidden chain-of-thought. No claims beyond
the dynamic request and prior tool evidence."""

_TOOL_NAME_BY_ID = {
    "repo.search": "repo_search",
    "repo.read": "repo_read",
    "repo.public_test": "repo_public_test",
    "repo.edit": "repo_edit",
    "repo.diff": "repo_diff",
}
_TOOL_ID_BY_NAME = {name: tool_id for tool_id, name in _TOOL_NAME_BY_ID.items()}


OpenAIResponsesClientFactory = Callable[..., Any]


class OpenAIResponsesProvider:
    """Harness-owned OpenAI Responses adapter for the controlled provider boundary."""

    def __init__(
        self,
        profile: HarnessDeploymentProfile,
        *,
        deployment: DeploymentIdentity | None = None,
        client_factory: OpenAIResponsesClientFactory | None = None,
    ) -> None:
        normalized_profile = HarnessDeploymentProfile.model_validate(profile)
        if normalized_profile.provider != "openai":
            raise ValueError("OpenAI Responses provider requires an openai deployment profile")
        if deployment is not None:
            normalized_profile.validate_deployment_identity(
                DeploymentIdentity.model_validate(deployment)
            )
        self._profile = normalized_profile
        self.deployment_identity = normalized_profile.to_deployment_identity()
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._sent_count = 0
        self._failed_after_send = False

    @property
    def execution_provenance(self) -> ProviderExecutionProvenance:
        with self._lock:
            sent_count = self._sent_count
            failed = self._failed_after_send
        if sent_count == 0:
            status = "not_run"
        elif failed:
            status = "failed"
        else:
            status = "completed"
        return ProviderExecutionProvenance(
            execution_mode="live_provider",
            live_inference_status=status,
            real_inference_requests_sent=sent_count,
        )

    def cancel(self, reservation_id: str) -> None:
        # The synchronous Responses request does not expose a response id before
        # completion. The controller's cancellation event remains the source of
        # local deadline/cancellation truth for this foreground adapter.
        return None

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        self._raise_if_control_stopped(control, request_sent=False)
        base_url = self._resolve_base_url()
        payload = self._safe_response_payload(request)
        api_key = self._resolve_api_key(credential_reference)
        timeout_s = self._timeout_seconds(control)
        client = self._build_client(api_key=api_key, base_url=base_url, timeout_s=timeout_s)
        self._raise_if_control_stopped(control, request_sent=False)

        self._mark_sent()
        try:
            response = client.responses.create(**payload)
        except ProviderInvocationError:
            self._mark_failed_after_send()
            raise
        except Exception as exc:
            self._mark_failed_after_send()
            usage = self._usage_from_exception(exc)
            metadata = _provider_error_metadata(exc)
            raise ProviderInvocationError(
                request_sent=True,
                usage=usage,
                **metadata,
            ) from None

        usage = self._usage_from_response(response)
        try:
            self._raise_if_control_stopped(control, request_sent=True, usage=usage)
            if not self._response_completed(response):
                raise ProviderInvocationError(request_sent=True, usage=usage)
            turn = self._actor_turn_from_response(response, request)
        except ProviderInvocationError:
            self._mark_failed_after_send()
            raise
        except Exception as exc:
            self._mark_failed_after_send()
            raise ProviderInvocationError(request_sent=True, usage=usage) from exc
        return ProviderInvocation(response=turn, usage=usage)

    def provider_request_reservation(self, request: Any) -> ProviderRequestReservation:
        payload = self._response_payload(request)
        # OpenAI tokenization is byte-backed: every ordinary token consumes at
        # least one UTF-8 byte. The compact JSON byte length of the complete
        # request therefore upper-bounds its instructions, input, tool
        # definitions, structured-output schema, and public transport framing.
        # Retain the caller's declared estimate when it is larger, then cover
        # Responses-only sentinel/framing tokens that are absent from the
        # public request body with one fixed, deliberately generous margin.
        input_tokens = (
            max(
                int(getattr(request, "input_token_estimate", 0)),
                _utf8_json_bytes(payload),
            )
            + _OPENAI_HIDDEN_FRAMING_TOKEN_MARGIN
        )
        if _requires_short_context_pricing_guard(self._profile.model) and (
            input_tokens > _GPT56_SHORT_CONTEXT_MAX_RESERVED_INPUT_TOKENS
        ):
            raise ValueError("frozen short-context GPT-5.6 pricing cannot cover this request")
        cache_prefix_tokens = 0
        if self._profile.prompt_cache_policy.mode == "explicit":
            # The breakpoint follows the first static input block. Bound the
            # corresponding cache read/write prefix using the same byte-level
            # rule over a complete request shape with dynamic content removed.
            cache_prefix_tokens = min(
                input_tokens,
                _utf8_json_bytes(_cache_prefix_payload(payload))
                + _OPENAI_HIDDEN_FRAMING_TOKEN_MARGIN,
            )
        max_known_cost_usd = self._max_known_cost_usd(
            input_tokens=input_tokens,
            output_tokens=payload["max_output_tokens"],
            max_cached_tokens=cache_prefix_tokens,
            max_cache_write_tokens=cache_prefix_tokens,
        )
        return ProviderRequestReservation(
            input_tokens=input_tokens,
            max_output_tokens=payload["max_output_tokens"],
            max_cached_tokens=cache_prefix_tokens,
            max_cache_write_tokens=cache_prefix_tokens,
            max_known_cost_usd=max_known_cost_usd,
        )

    def _safe_response_payload(self, request: Any) -> dict[str, Any]:
        try:
            return self._response_payload(request)
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise ProviderInvocationError(request_sent=False) from exc

    def _build_client(self, *, api_key: str, base_url: str, timeout_s: float) -> Any:
        factory = self._client_factory or _default_client_factory
        try:
            return factory(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_s,
                max_retries=0,
            )
        except Exception as exc:
            raise ProviderInvocationError(request_sent=False) from exc

    def _resolve_api_key(self, credential_reference: CredentialReference | None) -> str:
        try:
            self._validate_credential_reference(credential_reference)
            assert credential_reference is not None
            direct = (
                _read_env_value(credential_reference.api_key_env)
                if credential_reference.api_key_env
                else None
            )
            key_file = (
                _read_env_value(credential_reference.api_key_file_env)
                if credential_reference.api_key_file_env
                else None
            )
            if bool(direct) == bool(key_file):
                raise ValueError("expected exactly one credential source")
            if direct:
                return _normalize_secret_value(direct)
            assert key_file is not None
            return _read_api_key_file(key_file)
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise ProviderInvocationError(request_sent=False) from exc

    def _validate_credential_reference(
        self,
        credential_reference: CredentialReference | None,
    ) -> None:
        if credential_reference is None:
            raise ValueError("live OpenAI provider requires a credential reference")
        expected = CredentialReference(
            provider_name=self._profile.provider,
            api_key_env=self._profile.endpoint.api_key_env,
            api_key_file_env=self._profile.endpoint.api_key_file_env,
        )
        if credential_reference != expected:
            raise ValueError("credential reference does not match the frozen profile")

    def _resolve_base_url(self) -> str:
        try:
            endpoint = self._profile.endpoint
            if endpoint.base_url is not None:
                return _validate_base_url(endpoint.base_url)
            if endpoint.base_url_env is None:
                raise ValueError("missing base URL reference")
            value = _read_env_value(endpoint.base_url_env)
            if value is None:
                raise ValueError("missing base URL environment value")
            return _validate_base_url(value)
        except Exception as exc:
            raise ProviderInvocationError(request_sent=False) from exc

    def _response_payload(self, request: Any) -> dict[str, Any]:
        request_payload = _json_public_payload(request)
        decoding = self._profile.decoding_policy
        prompt_cache = self._profile.prompt_cache_policy
        payload: dict[str, Any] = {
            "model": self._profile.model,
            "instructions": _STATIC_INSTRUCTIONS,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        self._static_content_block(prompt_cache),
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {
                                    "actor_request": request_payload,
                                    "allowed_local_tools": [
                                        str(tool_id)
                                        for tool_id in getattr(request, "allowed_tool_ids", ())
                                    ],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agintor_terminal_turn",
                    "strict": True,
                    "schema": _terminal_turn_schema(),
                },
                "verbosity": decoding.text_verbosity,
            },
            "max_output_tokens": min(request.max_output_tokens, decoding.max_output_tokens),
            "temperature": decoding.temperature,
            "top_p": decoding.top_p,
            "service_tier": decoding.service_tier,
            "store": decoding.store,
            "parallel_tool_calls": decoding.parallel_tool_calls,
            "stream": False,
        }
        if decoding.reasoning_effort is not None:
            payload["reasoning"] = {"effort": decoding.reasoning_effort}
        if prompt_cache.mode == "explicit":
            payload["prompt_cache_key"] = prompt_cache.prompt_cache_key
            payload["prompt_cache_options"] = {
                "mode": "explicit",
                "ttl": prompt_cache.ttl,
            }
        else:
            # GPT-5.6 defaults to implicit cache writes. Explicit mode with no
            # breakpoint is the documented cache-off request shape.
            payload["prompt_cache_options"] = {
                "mode": "explicit",
                "ttl": "30m",
            }
        tools = _tool_definitions(request)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _static_content_block(prompt_cache: Any) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "input_text",
            "text": (
                "Agintor static-prefix cache breakpoint. The full fixed repo-repair "
                "actor contract is rendered in instructions before this block; "
                "dynamic actor request content follows this block."
            ),
        }
        if prompt_cache.mode == "explicit":
            block["prompt_cache_breakpoint"] = {"mode": "explicit"}
        return block

    def _usage_from_response(self, response: Any) -> ProviderUsageReport:
        usage = _lookup(response, "usage")
        if usage is None:
            return ProviderUsageReport.unknown()
        try:
            input_tokens = _required_int(usage, "input_tokens")
            output_tokens = _required_int(usage, "output_tokens")
            response_id = _optional_str(response, "id")
            if response_id is None or not response_id.strip():
                raise ValueError("known usage requires a response id")
            details = _lookup(usage, "input_tokens_details") or {}
            cached_tokens = _optional_int(details, "cached_tokens") or 0
            cache_write_tokens = (
                _optional_int(details, "cache_write_tokens")
                or _optional_int(details, "cache_creation_tokens")
                or 0
            )
            if cached_tokens + cache_write_tokens > input_tokens:
                raise ValueError("cache token subcategories exceed total input tokens")
            return ProviderUsageReport(
                usage_status=UsageStatus.KNOWN,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_status=CostStatus.KNOWN,
                cost_usd=self._cost_usd(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens,
                ),
                response_id=response_id,
            )
        except Exception:
            return ProviderUsageReport.unknown()

    def _usage_from_exception(self, exc: Exception) -> ProviderUsageReport | None:
        response = getattr(exc, "response", None)
        if response is None:
            return None
        usage = self._usage_from_response(response)
        if usage.usage_status is UsageStatus.UNKNOWN or usage.cost_status is CostStatus.UNKNOWN:
            return None
        return usage

    def _cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
    ) -> float:
        schedule = self._profile.price_schedule
        if schedule.billing_mode == "free":
            return 0.0
        uncached_input = input_tokens - cached_tokens - cache_write_tokens
        cost = (
            uncached_input * schedule.input_usd_per_million_tokens
            + output_tokens * schedule.output_usd_per_million_tokens
            + cached_tokens * schedule.cached_input_usd_per_million_tokens
            + cache_write_tokens * schedule.cache_write_usd_per_million_tokens
        ) / 1_000_000
        return float(cost)

    def _max_known_cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        max_cached_tokens: int,
        max_cache_write_tokens: int,
    ) -> float:
        schedule = self._profile.price_schedule
        if schedule.billing_mode == "free":
            return 0.0

        input_rate = schedule.input_usd_per_million_tokens
        cost = input_tokens * input_rate + output_tokens * schedule.output_usd_per_million_tokens
        remaining_input = input_tokens
        cache_categories = sorted(
            (
                (max_cached_tokens, schedule.cached_input_usd_per_million_tokens),
                (max_cache_write_tokens, schedule.cache_write_usd_per_million_tokens),
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        for maximum_tokens, rate in cache_categories:
            if rate <= input_rate or remaining_input == 0:
                continue
            category_tokens = min(maximum_tokens, remaining_input)
            cost += category_tokens * (rate - input_rate)
            remaining_input -= category_tokens
        return float(cost / 1_000_000)

    @staticmethod
    def _response_completed(response: Any) -> bool:
        status = _lookup(response, "status")
        return status in (None, "completed")

    @staticmethod
    def _actor_turn_from_response(
        response: Any,
        request: Any,
    ) -> ActorToolRequest | ActorTerminalTurn:
        function_calls = _response_function_calls(response)
        if function_calls:
            if _optional_response_output_text(response) is not None:
                raise ValueError("function call response must not include terminal text")
            if len(function_calls) != 1:
                raise ValueError("expected exactly one function call")
            return _actor_tool_request_from_function_call(function_calls[0], request)
        text = _response_output_text(response)
        payload = json.loads(text)
        if not isinstance(payload, Mapping):
            raise ValueError("actor turn response must be a JSON object")
        turn_kind = payload.get("turn_kind")
        if turn_kind == "terminal":
            return _terminal_turn_from_payload(payload)
        raise ValueError("actor turn response requires turn_kind")

    @staticmethod
    def _timeout_seconds(control: ProviderCallControl) -> float:
        remaining = min(control.timeout_ms, control.remaining_ms())
        if remaining <= 0:
            raise ProviderInvocationError(request_sent=False, deadline_exceeded=True)
        return max(0.001, remaining / 1000.0)

    @staticmethod
    def _raise_if_control_stopped(
        control: ProviderCallControl,
        *,
        request_sent: bool,
        usage: ProviderUsageReport | None = None,
    ) -> None:
        if control.cancelled:
            raise ProviderInvocationError(
                request_sent=request_sent,
                usage=usage if request_sent else None,
                cancelled=True,
            )
        if control.remaining_ms() <= 0:
            raise ProviderInvocationError(
                request_sent=request_sent,
                usage=usage if request_sent else None,
                deadline_exceeded=True,
            )

    def _mark_sent(self) -> None:
        with self._lock:
            self._sent_count += 1

    def _mark_failed_after_send(self) -> None:
        with self._lock:
            if self._sent_count:
                self._failed_after_send = True


def _default_client_factory(**kwargs: Any) -> Any:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise ProviderInvocationError(request_sent=False) from exc
    return OpenAI(**kwargs)


def _requires_short_context_pricing_guard(model: str) -> bool:
    return str(model or "").strip().lower() in {"gpt-5.6-terra", "gpt-5.6-luna"}


def _read_env_value(name: str | None) -> str | None:
    if name is None:
        return None
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value


def _normalize_secret_value(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or "\x00" in normalized
        or "\r" in normalized
        or "\n" in normalized
        or _OPENAI_API_KEY_RE.fullmatch(normalized) is None
    ):
        raise ValueError("invalid credential value")
    return normalized


def _read_api_key_file(raw_path: str) -> str:
    path = Path(raw_path).expanduser()
    try:
        file_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("credential file is not a regular file") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    file_attributes = int(getattr(file_stat, "st_file_attributes", 0))
    if (
        path.is_symlink()
        or bool(reparse_flag and file_attributes & reparse_flag)
        or not stat.S_ISREG(file_stat.st_mode)
    ):
        raise ValueError("credential file is not a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > _MAX_API_KEY_FILE_BYTES:
        raise ValueError("credential file size is outside the allowed bounds")
    try:
        with resolved.open("rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                file_stat.st_dev,
                file_stat.st_ino,
            ):
                raise ValueError("credential file changed during validation")
            payload = handle.read(_MAX_API_KEY_FILE_BYTES + 1)
        if len(payload) > _MAX_API_KEY_FILE_BYTES:
            raise ValueError("credential file size is outside the allowed bounds")
        text = payload.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("credential file could not be read safely") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("credential file must contain exactly one credential")
    value = lines[0]
    if value.startswith("OPENAI_API_KEY="):
        value = value.removeprefix("OPENAI_API_KEY=").strip()
    return _normalize_secret_value(value)


def _validate_base_url(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in {_OPENAI_API_BASE_URL, f"{_OPENAI_API_BASE_URL}/"}:
        raise ValueError("live OpenAI base URL must be the canonical OpenAI API endpoint")
    return _OPENAI_API_BASE_URL


def _provider_error_metadata(exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    try:
        http_status = int(status) if status is not None else None
    except (TypeError, ValueError):
        http_status = None
    if http_status is not None and not 100 <= http_status <= 599:
        http_status = None
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if code is None and isinstance(body, Mapping):
        error = body.get("error")
        source = error if isinstance(error, Mapping) else body
        code = source.get("code") or source.get("type")
    request_id = getattr(exc, "request_id", None)
    return {
        "provider_error_type": _safe_provider_token(type(exc).__name__, maximum=96),
        "provider_error_code": _safe_provider_token(code, maximum=128),
        "provider_http_status": http_status,
        "provider_request_id": _safe_provider_token(request_id, maximum=128),
    }


def _safe_provider_token(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > maximum:
        return None
    if re.fullmatch(r"[A-Za-z0-9._:-]+", normalized) is None:
        return None
    return normalized


def _utf8_json_bytes(value: Any) -> int:
    """Return the byte-level token upper bound for a JSON request value."""

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(serialized.encode("utf-8"))


def _cache_prefix_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Render the complete request shape ending at the explicit cache breakpoint."""

    messages = payload.get("input")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("OpenAI request cache prefix requires exactly one message")
    message = messages[0]
    if not isinstance(message, Mapping):
        raise ValueError("OpenAI request cache prefix message must be an object")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 2:
        raise ValueError("OpenAI request cache prefix requires two content blocks")
    prefix_payload = dict(payload)
    prefix_payload["input"] = [
        {
            **message,
            "content": [content[0]],
        }
    ]
    return prefix_payload


def _json_public_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json", exclude_none=True)
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("OpenAI Responses provider requires a structured actor request")
    if not isinstance(payload, dict):
        raise TypeError("actor request payload must be a JSON object")
    assert_no_resolved_credentials(payload)
    assert_public_payload(payload)
    return payload


def _terminal_turn_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["turn_kind", "output"],
        "properties": {
            "turn_kind": {"type": "string", "enum": ["terminal"]},
            "output": {
                "type": "object",
                "additionalProperties": False,
                "required": ["output_text", "artifact_payload_entries", "final_patch"],
                "properties": {
                    "output_text": {"type": "string"},
                    "artifact_payload_entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["key", "value"],
                            "properties": {
                                "key": {"type": "string", "minLength": 1},
                                "value": {"type": "string"},
                            },
                        },
                    },
                    "final_patch": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                },
            },
        },
    }


def _tool_definitions(request: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool_id in getattr(request, "allowed_tool_ids", ()):
        normalized = str(tool_id)
        tool_name = _TOOL_NAME_BY_ID.get(normalized)
        if tool_name is None:
            raise ValueError(f"unsupported local tool id {normalized!r}")
        tools.append(
            {
                "type": "function",
                "name": tool_name,
                "description": (
                    f"Request the local Agintor trusted tool {normalized}. "
                    "The harness validates and executes the tool locally."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["arguments_json"],
                    "properties": {
                        "arguments_json": {
                            "type": "string",
                            "description": "A serialized JSON object of local tool arguments.",
                        },
                    },
                },
                "strict": True,
            }
        )
    return tools


def _response_function_calls(response: Any) -> list[Any]:
    calls: list[Any] = []
    for item in _lookup(response, "output") or ():
        if _lookup(item, "type") == "function_call":
            calls.append(item)
    return calls


def _actor_tool_request_from_function_call(
    call: Any,
    request: Any,
) -> ActorToolRequest:
    name = _optional_str(call, "name")
    tool_id = _TOOL_ID_BY_NAME.get(name or "")
    allowed = {str(tool_id) for tool_id in getattr(request, "allowed_tool_ids", ())}
    if tool_id is None or tool_id not in allowed:
        raise ValueError("function call does not map to an authorized local tool")
    arguments_payload = json.loads(_required_str(call, "arguments"))
    if not isinstance(arguments_payload, Mapping):
        raise ValueError("function call arguments must be a JSON object")
    arguments_json = arguments_payload.get("arguments_json")
    if not isinstance(arguments_json, str):
        raise ValueError("function call requires arguments_json")
    local_arguments = json.loads(arguments_json)
    if not isinstance(local_arguments, Mapping):
        raise ValueError("arguments_json must decode to a JSON object")
    request_id = _optional_str(call, "call_id") or _optional_str(call, "id")
    if request_id is None or not request_id.strip():
        raise ValueError("function call requires a request id")
    return ActorToolRequest(
        request_id=request_id,
        tool_id=tool_id,
        arguments=dict(local_arguments),
    )


def _terminal_turn_from_payload(payload: Mapping[str, Any]) -> ActorTerminalTurn:
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise ValueError("terminal output must be a JSON object")
    entries = output.get("artifact_payload_entries")
    if not isinstance(entries, list):
        raise ValueError("terminal output requires artifact_payload_entries")
    artifact_payloads: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("artifact payload entry must be an object")
        key = entry.get("key")
        value = entry.get("value")
        if not isinstance(key, str) or not key:
            raise ValueError("artifact payload entry key must be nonempty")
        if key in artifact_payloads:
            raise ValueError("artifact payload entry keys must be unique")
        if not isinstance(value, str):
            raise ValueError("artifact payload entry value must be a string")
        artifact_payloads[key] = value
    normalized = {
        "turn_kind": "terminal",
        "output": {
            "output_text": output.get("output_text"),
            "artifact_payloads": artifact_payloads,
            "final_patch": output.get("final_patch"),
        },
    }
    return ActorTerminalTurn.model_validate(normalized)


def _response_output_text(response: Any) -> str:
    text = _optional_response_output_text(response)
    if text is not None:
        return text
    raise ValueError("Responses API response did not include output text")


def _optional_response_output_text(response: Any) -> str | None:
    output_text = _lookup(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = _lookup(response, "output") or ()
    for item in output:
        content_items = _lookup(item, "content") or ()
        for content in content_items:
            text = _lookup(content, "text")
            if isinstance(text, str) and text.strip():
                return text
    return None


def _lookup(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _optional_str(value: Any, key: str) -> str | None:
    raw = _lookup(value, key)
    if raw is None:
        return None
    return str(raw)


def _required_str(value: Any, key: str) -> str:
    raw = _lookup(value, key)
    if raw is None:
        raise ValueError(f"missing string field {key}")
    text = str(raw)
    if not text:
        raise ValueError(f"empty string field {key}")
    return text


def _required_int(value: Any, key: str) -> int:
    raw = _lookup(value, key)
    if raw is None:
        raise ValueError(f"missing integer usage field {key}")
    integer = int(raw)
    if integer < 0:
        raise ValueError(f"negative integer usage field {key}")
    return integer


def _optional_int(value: Any, key: str) -> int | None:
    raw = _lookup(value, key)
    if raw is None:
        return None
    integer = int(raw)
    if integer < 0:
        raise ValueError(f"negative integer usage field {key}")
    return integer


__all__ = [
    "OpenAIResponsesClientFactory",
    "OpenAIResponsesProvider",
]
