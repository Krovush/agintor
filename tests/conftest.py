from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agintor.benchmarks import build_demo_suite
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider


@pytest.fixture()
def demo_suite():
    return build_demo_suite()


@pytest.fixture()
def runtime_dir(tmp_path: Path) -> Path:
    return init_runtime(tmp_path / "runtime")


@pytest.fixture()
def provider_local():
    return LocalDeterministicProvider()
