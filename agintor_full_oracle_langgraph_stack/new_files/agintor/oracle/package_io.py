from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts import OraclePackage, freeze_oracle_package, oracle_public_projection, oracle_sealed_projection
from ..utils import ensure_directory

ORACLE_PACKAGE_FILE = "oracle_package.json"
ORACLE_PUBLIC_VIEW_FILE = "oracle_public_view.json"
ORACLE_SEALED_VIEW_FILE = "oracle_sealed_view.json"
ORACLE_QA_REPORT_FILE = "oracle_qa_report.json"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def write_json(path: Path, payload: Any) -> Path:
    ensure_directory(path.parent)
    path.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_oracle_package(path: str | Path) -> OraclePackage:
    path = Path(path)
    if path.is_dir():
        path = path / ORACLE_PACKAGE_FILE
    return OraclePackage.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_oracle_package(package: OraclePackage, directory: str | Path, *, freeze: bool = True) -> OraclePackage:
    directory = ensure_directory(Path(directory))
    frozen = freeze_oracle_package(package) if freeze else OraclePackage.model_validate(package)
    write_json(directory / ORACLE_PACKAGE_FILE, frozen)
    write_json(directory / ORACLE_PUBLIC_VIEW_FILE, oracle_public_projection(frozen))
    write_json(directory / ORACLE_SEALED_VIEW_FILE, oracle_sealed_projection(frozen))
    return frozen


def write_public_projection(package: OraclePackage, path: str | Path) -> Path:
    return write_json(Path(path), oracle_public_projection(package))


def write_sealed_projection(package: OraclePackage, path: str | Path) -> Path:
    return write_json(Path(path), oracle_sealed_projection(package))


__all__ = [
    "ORACLE_PACKAGE_FILE",
    "ORACLE_PUBLIC_VIEW_FILE",
    "ORACLE_QA_REPORT_FILE",
    "ORACLE_SEALED_VIEW_FILE",
    "load_oracle_package",
    "write_json",
    "write_oracle_package",
    "write_public_projection",
    "write_sealed_projection",
]
