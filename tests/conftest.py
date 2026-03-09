from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_ARTIFACTS_ROOT = ROOT / "tests" / "_artifacts"
TEST_RUNS_ROOT = TEST_ARTIFACTS_ROOT / "runs"
SESSION_RUN_ID = f"r{time.strftime('%m%d%H%M%S')}_{os.getpid():x}_{hashlib.sha1(str(time.time_ns()).encode('utf-8')).hexdigest()[:4]}"
SESSION_RUN_ROOT = TEST_RUNS_ROOT / SESSION_RUN_ID
TEST_SYSTEM_TEMP_ROOT = SESSION_RUN_ROOT / "system_tmp"


def _ensure_clean_directory(path: Path) -> Path:
    target = path
    if path.exists():
        try:
            shutil.rmtree(path)
        except (OSError, PermissionError):
            target = path.parent / f"{path.name}_{time.time_ns()}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _sanitize_path_fragment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "artifact"


def _artifact_bucket(node: pytest.Node) -> str:
    module_name = Path(str(node.path)).stem
    if module_name.startswith("test_"):
        module_name = module_name[5:]
    return _sanitize_path_fragment(module_name)


def _artifact_leaf(node: pytest.Node) -> str:
    digest = hashlib.sha1(node.nodeid.encode("utf-8")).hexdigest()[:8]
    return digest


def _write_session_manifest() -> None:
    SESSION_RUN_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = SESSION_RUN_ROOT / "session.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": SESSION_RUN_ID,
                "created_at_unix_ns": time.time_ns(),
                "pid": os.getpid(),
                "artifacts_root": str(SESSION_RUN_ROOT),
                "system_temp_root": str(TEST_SYSTEM_TEMP_ROOT),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _prune_noncanonical_run_dirs() -> None:
    if not TEST_RUNS_ROOT.exists():
        return
    for child in TEST_RUNS_ROOT.iterdir():
        if not child.is_dir():
            continue
        if child.name == SESSION_RUN_ID or child.name.startswith("r"):
            continue
        try:
            shutil.rmtree(child)
        except (OSError, PermissionError):
            pass


TEST_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
TEST_SYSTEM_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(TEST_SYSTEM_TEMP_ROOT)
os.environ["TEMP"] = str(TEST_SYSTEM_TEMP_ROOT)
os.environ["TMP"] = str(TEST_SYSTEM_TEMP_ROOT)
tempfile.tempdir = str(TEST_SYSTEM_TEMP_ROOT)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agintor.benchmarks import build_demo_suite
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider


def pytest_sessionstart(session: pytest.Session) -> None:
    _prune_noncanonical_run_dirs()
    _write_session_manifest()
    _ensure_clean_directory(TEST_SYSTEM_TEMP_ROOT)


@pytest.fixture(scope="session")
def artifacts_root() -> Path:
    SESSION_RUN_ROOT.mkdir(parents=True, exist_ok=True)
    return SESSION_RUN_ROOT


@pytest.fixture()
def tmp_path(request: pytest.FixtureRequest, artifacts_root: Path) -> Path:
    test_dir = artifacts_root / _artifact_bucket(request.node) / _artifact_leaf(request.node)
    return _ensure_clean_directory(test_dir)


@pytest.fixture()
def demo_suite():
    return build_demo_suite()


@pytest.fixture()
def runtime_dir(tmp_path: Path) -> Path:
    return init_runtime(tmp_path / "runtime")


@pytest.fixture()
def provider_local():
    return LocalDeterministicProvider()
