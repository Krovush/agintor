from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import DeploymentIdentity
from ..core.identity import canonical_identity_digest, provider_config_digest
from ..isolation.commands import IsolatedCommandPolicy


_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PINNED_IMAGE_RE = re.compile(r"^[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}$")
_SECRET_ENV_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")


class HarnessDeploymentProfileError(ValueError):
    """Raised when an F1 harness deployment profile is not fully frozen."""


class HarnessDeploymentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _require_env_name(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = str(value or "").strip().upper()
    if not _ENV_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an environment variable name")
    return normalized


def _require_nonsecret_env_name(value: str, field_name: str) -> str:
    normalized = _require_env_name(value, field_name)
    assert normalized is not None
    if any(marker in normalized for marker in _SECRET_ENV_MARKERS):
        raise ValueError(f"{field_name} may not contain credential-like environment names")
    return normalized


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


class HarnessProviderEndpoint(HarnessDeploymentModel):
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    api_key_file_env: str | None = None

    @field_validator("base_url_env", "api_key_env", "api_key_file_env")
    @classmethod
    def validate_env_reference(cls, value: str | None, info: Any) -> str | None:
        return _require_env_name(value, info.field_name)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL, not a path or secret value")
        if "\x00" in normalized or "@" in normalized:
            raise ValueError("base_url may not contain embedded credentials")
        return normalized

    @model_validator(mode="after")
    def validate_endpoint(self) -> "HarnessProviderEndpoint":
        if (self.base_url is None) == (self.base_url_env is None):
            raise ValueError("provide exactly one frozen base_url or base_url_env reference")
        if not self.api_key_env and not self.api_key_file_env:
            raise ValueError("credential references must name an api key or key-file env var")
        return self


class HarnessDecodingPolicy(HarnessDeploymentModel):
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    max_output_tokens: int = Field(gt=0)
    reasoning_effort: str | None = None
    service_tier: Literal["default"] = "default"
    store: Literal[False] = False
    parallel_tool_calls: Literal[False] = False
    text_verbosity: Literal["low", "medium", "high"] = "low"

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("reasoning_effort may not be empty")
        return normalized


class HarnessPromptCachePolicy(HarnessDeploymentModel):
    """Frozen prompt-cache behavior for a live harness deployment."""

    mode: Literal["disabled", "explicit"] = "disabled"
    prompt_cache_key: str | None = None
    ttl: Literal["30m"] | None = None
    breakpoint: Literal["static_prefix"] | None = None
    minimum_prefix_tokens: Literal[1024] = 1024
    maximum_breakpoints: Literal[1] = 1

    @field_validator("prompt_cache_key")
    @classmethod
    def validate_prompt_cache_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("prompt_cache_key must contain 1 to 128 characters")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
            raise ValueError("prompt_cache_key may not contain control characters")
        return normalized

    @model_validator(mode="after")
    def validate_cache_policy(self) -> "HarnessPromptCachePolicy":
        configured = (self.prompt_cache_key, self.ttl, self.breakpoint)
        if self.mode == "disabled" and any(value is not None for value in configured):
            raise ValueError("disabled prompt caching may not retain cache-write settings")
        if self.mode == "explicit" and configured != (
            self.prompt_cache_key,
            "30m",
            "static_prefix",
        ):
            raise ValueError(
                "explicit prompt caching requires a key, 30m TTL, and static_prefix breakpoint"
            )
        if self.mode == "explicit" and self.prompt_cache_key is None:
            raise ValueError("explicit prompt caching requires prompt_cache_key")
        return self


class HarnessUsdPriceSchedule(HarnessDeploymentModel):
    billing_mode: Literal["paid", "free"] = "paid"
    input_usd_per_million_tokens: float = Field(ge=0.0)
    output_usd_per_million_tokens: float = Field(ge=0.0)
    cached_input_usd_per_million_tokens: float = Field(ge=0.0)
    cache_write_usd_per_million_tokens: float = Field(default=0.0, ge=0.0)
    provider_policy_justification: str | None = None

    @model_validator(mode="after")
    def validate_known_pricing(self) -> "HarnessUsdPriceSchedule":
        if self.billing_mode == "paid":
            if self.input_usd_per_million_tokens <= 0.0 or self.output_usd_per_million_tokens <= 0.0:
                raise ValueError("paid provider profiles require positive input and output USD rates")
            if self.provider_policy_justification is not None:
                raise ValueError("paid provider pricing may not use free-policy justification")
        else:
            if not str(self.provider_policy_justification or "").strip():
                raise ValueError("free billing mode requires provider-policy justification")
        return self


class HarnessCommandContainerPolicy(HarnessDeploymentModel):
    network: Literal["none"] = "none"
    image: str
    user: str = "65532:65532"
    timeout_s: float = Field(gt=0.0, le=3600.0)
    memory_bytes: int = Field(ge=32 * 1024 * 1024)
    cpu_count: float = Field(gt=0.0, le=64.0)
    pids_limit: int = Field(ge=8, le=4096)
    output_bytes: int = Field(ge=1024, le=64_000_000)
    tmpfs_bytes: int = Field(ge=1024 * 1024)
    nofile_limit: int = Field(ge=32, le=65536)
    environment_allowlist: tuple[str, ...] = ("LANG", "LC_ALL", "PYTHONHASHSEED", "TZ")

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _PINNED_IMAGE_RE.fullmatch(normalized):
            raise ValueError("command container image must be pinned as image@sha256:<64 hex>")
        return normalized

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        normalized = str(value or "").strip()
        match = re.fullmatch(r"([0-9]+):([0-9]+)", normalized)
        if match is None or int(match.group(1)) == 0 or int(match.group(2)) == 0:
            raise ValueError("command container user must be numeric non-root uid:gid")
        return normalized

    @field_validator("environment_allowlist")
    @classmethod
    def validate_environment_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(_require_nonsecret_env_name(item, "environment_allowlist") for item in value))
        if len(normalized) != len(set(normalized)):
            raise ValueError("environment_allowlist may not contain duplicates")
        return normalized

    def to_isolated_command_policy(self) -> IsolatedCommandPolicy:
        return IsolatedCommandPolicy(
            image=self.image,
            user=self.user,
            timeout_s=self.timeout_s,
            memory_bytes=self.memory_bytes,
            cpu_count=self.cpu_count,
            pids_limit=self.pids_limit,
            output_bytes=self.output_bytes,
            tmpfs_bytes=self.tmpfs_bytes,
            nofile_limit=self.nofile_limit,
            environment_allowlist=frozenset(self.environment_allowlist),
        )


class HarnessDeploymentProfile(HarnessDeploymentModel):
    runtime_kind: Literal["harness"] = "harness"
    deployment_id: str
    provider: str
    model: str
    endpoint: HarnessProviderEndpoint
    decoding_policy: HarnessDecodingPolicy
    prompt_cache_policy: HarnessPromptCachePolicy = Field(
        default_factory=HarnessPromptCachePolicy
    )
    price_schedule: HarnessUsdPriceSchedule
    command_container_policy: HarnessCommandContainerPolicy
    provider_config_digest: str = ""
    decoding_policy_digest: str = ""
    price_schedule_digest: str = ""
    command_container_policy_digest: str = ""

    @field_validator("deployment_id", "provider", "model")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{info.field_name} may not be empty")
        return normalized

    @field_validator(
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if value == "":
            return value
        return _require_digest(value, info.field_name)

    def provider_config_payload(self) -> dict[str, Any]:
        return {
            "runtime_kind": self.runtime_kind,
            "deployment_id": self.deployment_id,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint.model_dump(mode="json", exclude_none=True),
            "prompt_cache_policy": self.prompt_cache_policy.model_dump(
                mode="json", exclude_none=True
            ),
        }

    def decoding_policy_payload(self) -> dict[str, Any]:
        return self.decoding_policy.model_dump(mode="json", exclude_none=True)

    def price_schedule_payload(self) -> dict[str, Any]:
        return self.price_schedule.model_dump(mode="json")

    def command_container_policy_payload(self) -> dict[str, Any]:
        return self.command_container_policy.model_dump(mode="json")

    def profile_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={
                "provider_config_digest",
                "decoding_policy_digest",
                "price_schedule_digest",
                "command_container_policy_digest",
            },
        )

    def profile_digest(self) -> str:
        return canonical_identity_digest(self.profile_payload(), domain="harness-deployment-profile")

    @model_validator(mode="after")
    def bind_digests(self) -> "HarnessDeploymentProfile":
        if (
            self.prompt_cache_policy.mode == "explicit"
            and self.price_schedule.billing_mode == "paid"
            and self.price_schedule.cache_write_usd_per_million_tokens <= 0.0
        ):
            raise ValueError(
                "paid explicit prompt caching requires a positive cache-write USD rate"
            )
        computed_provider = provider_config_digest(self.provider_config_payload())
        computed_decoding = canonical_identity_digest(
            self.decoding_policy_payload(),
            domain="harness-decoding-policy",
        )
        computed_price = canonical_identity_digest(
            self.price_schedule_payload(),
            domain="harness-price-schedule",
        )
        computed_command = canonical_identity_digest(
            self.command_container_policy_payload(),
            domain="harness-command-container-policy",
        )
        updates = {
            "provider_config_digest": computed_provider,
            "decoding_policy_digest": computed_decoding,
            "price_schedule_digest": computed_price,
            "command_container_policy_digest": computed_command,
        }
        for field_name, computed in updates.items():
            current = getattr(self, field_name)
            if current and current != computed:
                raise ValueError(f"{field_name} does not match the frozen deployment profile")
            if not current:
                object.__setattr__(self, field_name, computed)
        return self

    def to_deployment_identity(self) -> DeploymentIdentity:
        return DeploymentIdentity(
            deployment_id=self.deployment_id,
            provider=self.provider,
            model=self.model,
            provider_config_digest=self.provider_config_digest,
            decoding_policy_digest=self.decoding_policy_digest,
            price_schedule_digest=self.price_schedule_digest,
            command_container_policy_digest=self.command_container_policy_digest,
        )

    def validate_deployment_identity(self, deployment: DeploymentIdentity) -> None:
        expected = self.to_deployment_identity()
        if deployment != expected:
            raise HarnessDeploymentProfileError("deployment identity does not match HarnessDeploymentProfile")


def harness_deployment_profile_digest(profile: HarnessDeploymentProfile) -> str:
    return profile.profile_digest()


__all__ = [
    "HarnessCommandContainerPolicy",
    "HarnessDecodingPolicy",
    "HarnessDeploymentProfile",
    "HarnessDeploymentProfileError",
    "HarnessProviderEndpoint",
    "HarnessPromptCachePolicy",
    "HarnessUsdPriceSchedule",
    "harness_deployment_profile_digest",
]
