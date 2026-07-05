from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..contracts import OraclePackage
from ..contracts.oracle import oracle_package_hash, oracle_public_view_hash, oracle_sealed_view_hash
from ..utils import ensure_directory, stable_hash
from .projections import public_oracle_projection, sealed_oracle_projection

ORACLE_PACKAGE_FILE = "package.json"
ORACLE_PUBLIC_FILE = "public.json"
ORACLE_SEALED_FILE = "sealed.json"
ORACLE_LOCK_FILE = "manifest.json"


def canonical_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return canonical_jsonable(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): canonical_jsonable(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical_jsonable(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonical_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_oracle_package_hash(package: OraclePackage | Mapping[str, Any]) -> str:
    return oracle_package_hash(package)


def finalize_oracle_package(package: OraclePackage) -> OraclePackage:
    public_payload = public_oracle_projection(package)
    sealed_payload = sealed_oracle_projection(package)
    public_hash = oracle_public_view_hash(package)
    sealed_hash = oracle_sealed_view_hash(package)
    package_hash = oracle_package_hash(package, assume_projection_hashes=(public_hash, sealed_hash))
    return package.model_copy(
        update={
            "package_hash": package_hash,
            "public_view_hash": public_hash,
            "sealed_view_hash": sealed_hash,
            "frozen": True,
        },
        deep=True,
    )


def write_oracle_package(package: OraclePackage, package_dir: str | Path) -> OraclePackage:
    frozen = finalize_oracle_package(package)
    root = ensure_directory(Path(package_dir))
    (root / ORACLE_PACKAGE_FILE).write_text(json.dumps(frozen.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
    (root / ORACLE_PUBLIC_FILE).write_text(json.dumps(public_oracle_projection(frozen), indent=2, sort_keys=True), encoding="utf-8")
    (root / ORACLE_SEALED_FILE).write_text(json.dumps(sealed_oracle_projection(frozen), indent=2, sort_keys=True), encoding="utf-8")
    lock = {
        "package_id": frozen.package_id,
        "package_hash": frozen.package_hash,
        "public_view_hash": frozen.public_view_hash,
        "sealed_view_hash": frozen.sealed_view_hash,
        "goal_id": frozen.goal_id,
        "runtime_spec_digest": frozen.runtime_spec_digest,
    }
    (root / ORACLE_LOCK_FILE).write_text(json.dumps(lock, indent=2, sort_keys=True), encoding="utf-8")
    return frozen


def load_oracle_package(path: str | Path) -> OraclePackage:
    source = Path(path)
    package_path = source / ORACLE_PACKAGE_FILE if source.is_dir() else source
    return OraclePackage.model_validate(json.loads(package_path.read_text(encoding="utf-8")))


def assert_package_lock_matches(package: OraclePackage, package_dir: str | Path) -> None:
    lock_path = Path(package_dir) / ORACLE_LOCK_FILE
    if not lock_path.exists():
        raise ValueError(f"missing oracle package lockfile at {lock_path}")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected = finalize_oracle_package(package)
    for key in ("package_hash", "public_view_hash", "sealed_view_hash"):
        if str(lock.get(key, "")) != str(getattr(expected, key)):
            raise ValueError(f"oracle package lock mismatch for {key}")


__all__ = [
    "ORACLE_LOCK_FILE",
    "ORACLE_PACKAGE_FILE",
    "ORACLE_PUBLIC_FILE",
    "ORACLE_SEALED_FILE",
    "assert_package_lock_matches",
    "canonical_json",
    "canonical_jsonable",
    "compute_oracle_package_hash",
    "finalize_oracle_package",
    "load_oracle_package",
    "write_oracle_package",
]
