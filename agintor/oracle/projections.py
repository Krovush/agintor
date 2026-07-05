from __future__ import annotations

from typing import Any

from ..contracts.oracle import oracle_public_projection, oracle_sealed_projection


def public_oracle_projection(package: Any) -> dict[str, Any]:
    return oracle_public_projection(package)


def sealed_oracle_projection(package: Any) -> dict[str, Any]:
    return oracle_sealed_projection(package)

__all__ = ["public_oracle_projection", "sealed_oracle_projection"]
