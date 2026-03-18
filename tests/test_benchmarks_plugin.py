from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.benchmarks import (
    BenchmarkSuite,
    load_suite,
    register_suite_provider,
    unregister_suite_provider,
)
from agintor.schemas import BenchmarkTask


def _minimal_task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="top",
        prompt="demo",
        task_type="structured_ops",
        operations=[],
        expected={"ok": True},
        verifier_type="json_exact",
    )


def test_load_suite_supports_registered_plugin_provider() -> None:
    def provider(_: str) -> BenchmarkSuite:
        task = _minimal_task("plugin.task")
        return BenchmarkSuite(name="plugin_suite", train=[task], val=[task], test=[task], proxy=[task])

    register_suite_provider("toy", provider)
    try:
        suite = load_suite("toy")
    finally:
        unregister_suite_provider("toy")

    assert suite.name == "plugin_suite"
    assert suite.train[0].task_id == "plugin.task"


def test_load_suite_supports_module_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_file = tmp_path / "suite_plugin_mod.py"
    plugin_file.write_text(
        """
from agintor.benchmarks import BenchmarkSuite
from agintor.schemas import BenchmarkTask

def build_suite():
    task = BenchmarkTask(task_id='module.task', family='top', prompt='p', task_type='structured_ops', operations=[], expected={'ok': True}, verifier_type='json_exact')
    return BenchmarkSuite(name='module_suite', train=[task], val=[task], test=[task], proxy=[task])
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    suite = load_suite("plugin:suite_plugin_mod:build_suite")
    assert suite.name == "module_suite"
    assert suite.train[0].task_id == "module.task"


def test_load_suite_rejects_unknown_schema_version(tmp_path: Path) -> None:
    payload = {
        "name": "bad_schema",
        "schema_version": "agintor.benchmark.v999",
        "train": [],
        "val": [],
        "test": [],
        "proxy": [],
    }
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_suite(str(suite_path))
