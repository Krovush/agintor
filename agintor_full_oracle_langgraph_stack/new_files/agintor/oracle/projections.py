from __future__ import annotations

from typing import Any

from ..contracts import (
    BenchmarkTask,
    OraclePackage,
    oracle_public_projection,
    oracle_runtime_visible_tasks_by_partition,
    oracle_sealed_projection,
    oracle_tasks_by_partition,
)


def public_package_view(package: OraclePackage) -> dict[str, Any]:
    return oracle_public_projection(package)


def sealed_package_view(package: OraclePackage) -> dict[str, Any]:
    return oracle_sealed_projection(package)


def runtime_visible_tasks(package: OraclePackage, partition: str = "train") -> list[BenchmarkTask]:
    return oracle_runtime_visible_tasks_by_partition(package, partition)


def evaluator_sealed_tasks(package: OraclePackage, partition: str = "train") -> list[BenchmarkTask]:
    return oracle_tasks_by_partition(package, partition)


def assert_no_sealed_material(public_view: dict[str, Any], forbidden_keys: list[str] | None = None) -> None:
    forbidden = set(forbidden_keys or ["private_expected", "private_answer_ref", "hidden_tests", "promotion_threshold"])

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if key_text in forbidden or key_text.startswith("private_"):
                    raise ValueError(f"sealed key {key_text!r} leaked at {path or '<root>'}")
                walk(item, f"{path}.{key_text}" if path else key_text)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(public_view)


__all__ = [
    "assert_no_sealed_material",
    "evaluator_sealed_tasks",
    "public_package_view",
    "runtime_visible_tasks",
    "sealed_package_view",
]
