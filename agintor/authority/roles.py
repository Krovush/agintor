from __future__ import annotations

import os
from typing import Literal, cast


ProcessRole = Literal["factory", "runtime", "proposer", "evaluator"]
PROCESS_ROLE_ENV = "AGINTOR_PROCESS_ROLE"
PUBLIC_PROCESS_ROLES = frozenset({"factory", "runtime", "proposer"})


def current_process_role() -> ProcessRole | None:
    value = os.environ.get(PROCESS_ROLE_ENV, "").strip().casefold()
    if not value:
        return None
    if value not in {*PUBLIC_PROCESS_ROLES, "evaluator"}:
        raise RuntimeError(f"unsupported {PROCESS_ROLE_ENV} value {value!r}")
    return cast(ProcessRole, value)


def assert_sealed_authority(operation: str) -> None:
    role = current_process_role()
    if role in PUBLIC_PROCESS_ROLES:
        raise PermissionError(
            f"{role} process is not authorized to {operation}; sealed authority is evaluator-only"
        )


def assert_evaluator_contract_import_allowed() -> None:
    role = current_process_role()
    if role in PUBLIC_PROCESS_ROLES:
        raise ImportError(
            f"{role} process cannot import evaluator-only EvaluationContract code"
        )


__all__ = [
    "PROCESS_ROLE_ENV",
    "PUBLIC_PROCESS_ROLES",
    "ProcessRole",
    "assert_evaluator_contract_import_allowed",
    "assert_sealed_authority",
    "current_process_role",
]
