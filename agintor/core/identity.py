from __future__ import annotations

import dataclasses
import hashlib
import math
from enum import Enum
from pathlib import PurePath
from typing import Any, Mapping

from .versioning import CANONICAL_IDENTITY_VERSION


_FORMAT_PREFIX = CANONICAL_IDENTITY_VERSION.encode("ascii") + b"\0"


def _frame(tag: bytes, payload: bytes) -> bytes:
    """Return an unambiguous type-and-length framed value."""
    return tag + len(payload).to_bytes(8, byteorder="big", signed=False) + payload


def _qualified_type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _encode_sequence(tag: bytes, values: list[Any] | tuple[Any, ...], active: set[int]) -> bytes:
    payload = len(values).to_bytes(8, byteorder="big", signed=False)
    payload += b"".join(_encode(value, active) for value in values)
    return _frame(tag, payload)


def _encode_container(value: Any, active: set[int], encode: Any) -> bytes:
    object_id = id(value)
    if object_id in active:
        raise ValueError("canonical identities cannot encode cyclic values")
    active.add(object_id)
    try:
        return encode()
    finally:
        active.remove(object_id)


def _encode(value: Any, active: set[int]) -> bytes:
    if value is None:
        return _frame(b"n", b"")
    if isinstance(value, Enum):
        payload = _frame(b"c", _qualified_type_name(value).encode("utf-8"))
        payload += _encode(value.value, active)
        return _frame(b"e", payload)
    if isinstance(value, bool):
        return _frame(b"b", b"1" if value else b"0")
    if isinstance(value, int):
        return _frame(b"i", str(value).encode("ascii"))
    if isinstance(value, float):
        if math.isnan(value):
            payload = b"nan"
        else:
            payload = value.hex().encode("ascii")
        return _frame(b"f", payload)
    if isinstance(value, str):
        return _frame(b"s", value.encode("utf-8"))
    if isinstance(value, bytes):
        return _frame(b"y", value)
    if isinstance(value, PurePath):
        return _frame(b"p", value.as_posix().encode("utf-8"))

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        def encode_model() -> bytes:
            # Contract identities describe validated semantic content, not the
            # import namespace that loaded the model.  The same contract loaded
            # as ``agintor`` and as bundled ``agintor_runtime`` must match.
            return _encode(model_dump(mode="python"), active)

        return _encode_container(value, active, encode_model)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        def encode_dataclass() -> bytes:
            fields = {
                field.name: getattr(value, field.name)
                for field in dataclasses.fields(value)
            }
            return _encode(fields, active)

        return _encode_container(value, active, encode_dataclass)

    if isinstance(value, Mapping):
        def encode_mapping() -> bytes:
            entries = [
                (_encode(key, active), _encode(item, active))
                for key, item in value.items()
            ]
            entries.sort(key=lambda entry: (entry[0], entry[1]))
            payload = len(entries).to_bytes(8, byteorder="big", signed=False)
            payload += b"".join(key + item for key, item in entries)
            return _frame(b"m", payload)

        return _encode_container(value, active, encode_mapping)

    if isinstance(value, list):
        return _encode_container(value, active, lambda: _encode_sequence(b"l", value, active))
    if isinstance(value, tuple):
        return _encode_container(value, active, lambda: _encode_sequence(b"t", value, active))
    if isinstance(value, set):
        def encode_set() -> bytes:
            items = sorted(_encode(item, active) for item in value)
            payload = len(items).to_bytes(8, byteorder="big", signed=False) + b"".join(items)
            return _frame(b"u", payload)

        return _encode_container(value, active, encode_set)
    if isinstance(value, frozenset):
        def encode_frozen_set() -> bytes:
            items = sorted(_encode(item, active) for item in value)
            payload = len(items).to_bytes(8, byteorder="big", signed=False) + b"".join(items)
            return _frame(b"r", payload)

        return _encode_container(value, active, encode_frozen_set)

    raise TypeError(
        "canonical identity does not support values of type "
        f"{_qualified_type_name(value)}"
    )


def canonical_identity_bytes(value: Any) -> bytes:
    """Encode a supported value into deterministic, recursive typed bytes."""
    return _FORMAT_PREFIX + _encode(value, set())


def canonical_identity_digest(value: Any, *, domain: str) -> str:
    """Return a SHA-256 identity separated from every other identity domain."""
    normalized_domain = str(domain).strip()
    if not normalized_domain:
        raise ValueError("canonical identity domain may not be empty")
    payload = _FORMAT_PREFIX
    payload += _frame(b"D", normalized_domain.encode("utf-8"))
    payload += _encode(value, set())
    return hashlib.sha256(payload).hexdigest()


def task_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="task")


def protocol_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="protocol")


def composite_plan_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="composite-plan")


def environment_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="environment")


def provider_config_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="provider-config")


def transaction_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="transaction")


def evidence_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="evidence")


__all__ = [
    "canonical_identity_bytes",
    "canonical_identity_digest",
    "composite_plan_digest",
    "environment_digest",
    "evidence_digest",
    "protocol_digest",
    "provider_config_digest",
    "task_digest",
    "transaction_digest",
]
