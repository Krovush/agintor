from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


HARNESS_BUNDLE_PROFILE = "harness"
LEGACY_BUNDLE_PROFILE = "legacy"
HARNESS_KERNEL_CAPABILITY_FLAGS = (
    "repo_repair_harness_v1",
    "inspect",
    "solve",
    "explicit_adapter_registry",
    "deterministic_replay",
    "public_task_boundary",
    "isolated_public_verification",
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class HarnessKernelManifest(BaseModel):
    """Minimal content-addressed manifest understood by a harness-only bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_contract_version: str = Field(min_length=1)
    package_name: str
    entry_module: str
    files: dict[str, str] = Field(min_length=1)
    capability_flags: tuple[str, ...] = HARNESS_KERNEL_CAPABILITY_FLAGS

    @field_validator("package_name")
    @classmethod
    def validate_package_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _PACKAGE_RE.fullmatch(normalized):
            raise ValueError("kernel package_name must be a portable Python package name")
        return normalized

    @field_validator("entry_module")
    @classmethod
    def validate_entry_module(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _MODULE_RE.fullmatch(normalized):
            raise ValueError("kernel entry_module must be a portable Python module name")
        return normalized

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_path, raw_digest in value.items():
            path = str(raw_path or "").strip().replace("\\", "/")
            parts = tuple(part for part in path.split("/") if part)
            if not path or path.startswith("/") or ".." in parts or ":" in parts[0]:
                raise ValueError("kernel manifest file paths must be safe and relative")
            digest = str(raw_digest or "").strip().lower()
            if not _DIGEST_RE.fullmatch(digest):
                raise ValueError("kernel manifest file digests must be lowercase SHA-256 values")
            if path in normalized:
                raise ValueError("kernel manifest file paths must be unique")
            normalized[path] = digest
        return dict(sorted(normalized.items()))

    @field_validator("capability_flags")
    @classmethod
    def validate_capability_flags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(item or "").strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("kernel capability flags may not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("kernel capability flags may not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_entry_membership(self) -> "HarnessKernelManifest":
        expected_entry = f"{self.entry_module.replace('.', '/')}.py"
        if expected_entry not in self.files:
            raise ValueError("kernel entry_module is not declared in files")
        if not all(path.startswith(f"{self.package_name}/") for path in self.files):
            raise ValueError("kernel manifest files must stay within package_name")
        return self


__all__ = [
    "HARNESS_BUNDLE_PROFILE",
    "HARNESS_KERNEL_CAPABILITY_FLAGS",
    "LEGACY_BUNDLE_PROFILE",
    "HarnessKernelManifest",
]
