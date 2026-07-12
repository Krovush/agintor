from __future__ import annotations

from importlib import import_module

from ...core.versioning import RUNTIME_CONTRACT_VERSION
from .harness_manifest import (
    HARNESS_BUNDLE_PROFILE,
    HARNESS_KERNEL_CAPABILITY_FLAGS,
    LEGACY_BUNDLE_PROFILE,
    HarnessKernelManifest,
)

KERNEL_BUNDLE_DIR = "runtime_sdk"
KERNEL_MANIFEST_FILE = "kernel_manifest.json"
KERNEL_PACKAGE_NAME = "agintor_runtime"
KERNEL_CAPABILITY_FLAGS = [
    "inspect",
    "run_batch",
    "resume",
    "checkpoint_refs",
    "checkpoint_envelopes",
    "provider_usage",
    "trace_refs",
    "side_effect_receipts",
    "runtime_isolation",
]

_BUNDLE_EXPORTS = {
    "bundle_runtime_kernel",
    "kernel_manifest_path",
    "preview_kernel_manifest",
    "validate_kernel_bundle",
}


def __getattr__(name: str):
    if name in _BUNDLE_EXPORTS:
        bundle = import_module(f"{__name__}.bundle")
        value = getattr(bundle, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "HARNESS_BUNDLE_PROFILE",
    "HARNESS_KERNEL_CAPABILITY_FLAGS",
    "LEGACY_BUNDLE_PROFILE",
    "HarnessKernelManifest",
    "KERNEL_BUNDLE_DIR",
    "KERNEL_CAPABILITY_FLAGS",
    "KERNEL_MANIFEST_FILE",
    "KERNEL_PACKAGE_NAME",
    "RUNTIME_CONTRACT_VERSION",
    "bundle_runtime_kernel",
    "kernel_manifest_path",
    "preview_kernel_manifest",
    "validate_kernel_bundle",
]
