from __future__ import annotations

from typing import Any

from ..authority.roles import assert_sealed_authority
from ..contracts.oracle import oracle_public_projection, oracle_sealed_projection


def public_oracle_projection(package: Any) -> dict[str, Any]:
    return oracle_public_projection(package)


def sealed_oracle_projection(package: Any) -> dict[str, Any]:
    assert_sealed_authority("project a sealed Oracle package")
    return oracle_sealed_projection(package)

__all__ = ["public_oracle_projection", "sealed_oracle_projection"]
