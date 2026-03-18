from __future__ import annotations

import atexit
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.dont_write_bytecode = True

from agintor.benchmarks import build_demo_suite
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider


def _cleanup_repo_artifacts() -> None:
    cleanup_paths = [
        ROOT / "__pycache__",
        ROOT / ".pytest_cache",
        ROOT / "agintor" / "__pycache__",
        ROOT / "tests" / "__pycache__",
    ]
    for path in cleanup_paths:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    for path in ROOT.glob("pytest-cache-files-*"):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_repo_artifacts)


def pytest_sessionstart(session) -> None:
    del session
    sys.dont_write_bytecode = True


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    sys.dont_write_bytecode = True
    _cleanup_repo_artifacts()


def pytest_configure(config) -> None:
    del config
    sys.dont_write_bytecode = True


@pytest.fixture()
def demo_suite():
    return build_demo_suite()


@pytest.fixture()
def runtime_dir(tmp_path: Path) -> Path:
    return init_runtime(tmp_path / "runtime")


@pytest.fixture()
def provider_local():
    return LocalDeterministicProvider()
