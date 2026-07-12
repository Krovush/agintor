from __future__ import annotations

import os
import re
from collections.abc import Mapping


_CREDENTIAL_ENV_MARKERS = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "KEY_FILE",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
_API_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{12,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_REDACTION = "[REDACTED_CREDENTIAL]"


def redact_sensitive_text(
    value: object,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Remove resolved credential values from text crossing a public boundary."""

    text = str(value)
    source = os.environ if environment is None else environment
    candidates = {
        secret
        for name, secret in source.items()
        if secret
        and len(secret) >= 4
        and any(marker in str(name).upper() for marker in _CREDENTIAL_ENV_MARKERS)
    }
    for secret in sorted(candidates, key=len, reverse=True):
        text = text.replace(secret, _REDACTION)
    return _API_KEY_PATTERN.sub(_REDACTION, text)


__all__ = ["redact_sensitive_text"]
