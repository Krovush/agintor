from __future__ import annotations

import atexit
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.dont_write_bytecode = True

from agintor.artifacts import ArtifactAllocator
from agintor.benchmarks import build_demo_suite
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider

TEST_ARTIFACT_ALLOCATOR = ArtifactAllocator.resolve(repo_root=ROOT)


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


def _local_timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def _module_artifact_name(module_path: Path) -> str:
    try:
        relative = module_path.resolve().relative_to(TESTS_ROOT.resolve())
    except ValueError:
        relative = Path(module_path.name)
    return relative.as_posix().replace("/", "__").replace(".py", ".json")


def _path_snapshot(path: Path, *, max_entries: int = 64) -> dict[str, Any]:
    resolved = Path(path)
    payload: dict[str, Any] = {"path": str(resolved), "exists": resolved.exists()}
    if not resolved.exists():
        return payload
    if resolved.is_file():
        payload["kind"] = "file"
        payload["size"] = resolved.stat().st_size
        return payload
    payload["kind"] = "directory"
    entries: list[dict[str, Any]] = []
    for child in sorted(resolved.rglob("*")):
        rel = child.relative_to(resolved).as_posix()
        if child.is_dir():
            entries.append({"path": rel, "kind": "directory"})
        else:
            entries.append({"path": rel, "kind": "file", "size": child.stat().st_size})
        if len(entries) >= max_entries:
            payload["truncated"] = True
            break
    payload["entries"] = entries
    return payload


def _snapshot_funcarg_paths(item: pytest.Item) -> dict[str, Any]:
    snapshots: dict[str, Any] = {}
    for name, value in getattr(item, "funcargs", {}).items():
        if not isinstance(value, Path):
            continue
        if name not in {"tmp_path", "runtime_dir", "sandbox_cache_root"} and "workspace" not in name:
            continue
        snapshots[name] = _path_snapshot(value)
    return snapshots


@dataclass
class FailureArtifactManager:
    collected_modules: set[Path] = field(default_factory=set)
    failures_by_module: dict[Path, list[dict[str, Any]]] = field(default_factory=dict)
    session_started_at: str = field(default_factory=_local_timestamp)

    def register_module(self, module_path: Path) -> None:
        self.collected_modules.add(module_path.resolve())

    def record_failure(self, item: pytest.Item, report: pytest.TestReport) -> None:
        module_path = Path(str(item.path)).resolve()
        self.register_module(module_path)
        failure = {
            "nodeid": item.nodeid,
            "phase": report.when,
            "message": str(report.longreprtext or report.longrepr),
            "sections": [{"name": name, "content": content} for name, content in report.sections],
            "path_snapshots": _snapshot_funcarg_paths(item),
            "recorded_at": _local_timestamp(),
        }
        self.failures_by_module.setdefault(module_path, []).append(failure)

    def finalize(self) -> None:
        recent_dir = TEST_ARTIFACT_ALLOCATOR.timestamped_bucket(
            purpose="test_failures",
            prefix="run",
            within=timedelta(hours=1),
            create=bool(self.failures_by_module),
        )
        if recent_dir is None:
            return
        for module_path in self.collected_modules:
            target = recent_dir / _module_artifact_name(module_path)
            failures = self.failures_by_module.get(module_path, [])
            if failures:
                payload = {
                    "session_started_at": self.session_started_at,
                    "updated_at": _local_timestamp(),
                    "module_path": str(module_path),
                    "failures": failures,
                }
                target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            elif target.exists():
                target.unlink()
        if recent_dir.exists() and not any(recent_dir.iterdir()):
            recent_dir.rmdir()


def _failure_artifact_manager(config: pytest.Config) -> FailureArtifactManager:
    return config._agintor_failure_artifact_manager


atexit.register(_cleanup_repo_artifacts)


def pytest_sessionstart(session) -> None:
    del session
    sys.dont_write_bytecode = True


def pytest_sessionfinish(session, exitstatus) -> None:
    _failure_artifact_manager(session.config).finalize()
    del session, exitstatus
    sys.dont_write_bytecode = True
    _cleanup_repo_artifacts()


def pytest_configure(config) -> None:
    sys.dont_write_bytecode = True
    config._agintor_failure_artifact_manager = FailureArtifactManager()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    del call
    outcome = yield
    report = outcome.get_result()
    manager = _failure_artifact_manager(item.config)
    manager.register_module(Path(str(item.path)))
    if report.failed and not getattr(report, "wasxfail", False):
        manager.record_failure(item, report)


@pytest.fixture()
def demo_suite():
    return build_demo_suite()


@pytest.fixture(scope="session")
def sandbox_cache_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("agintor_sandbox_cache")


@pytest.fixture(autouse=True)
def _artifact_mode_env(monkeypatch: pytest.MonkeyPatch, sandbox_cache_root: Path) -> None:
    monkeypatch.setenv("AGINTOR_SANDBOX_CACHE_ROOT", str(sandbox_cache_root))
    sys.dont_write_bytecode = True


@pytest.fixture(scope="module")
def module_failure_artifact_bucket(request: pytest.FixtureRequest) -> None:
    _failure_artifact_manager(request.config).register_module(Path(str(request.node.path)))


@pytest.fixture(scope="session")
def runtime_template_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("agintor_runtime_template")
    return init_runtime(root / "runtime")


@pytest.fixture()
def runtime_dir(tmp_path: Path, runtime_template_dir: Path) -> Path:
    destination = tmp_path / "runtime"
    shutil.copytree(runtime_template_dir, destination)
    return destination


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if (
            "tests/test_evolution.py::" in nodeid
            and "test_evolution_engine_runs_smoke" not in nodeid
        ):
            item.add_marker(pytest.mark.heavy)
        if (
            "tests/test_runtime_builder.py::" in nodeid
            and "test_build_goal_" not in nodeid
            and "test_build_runtime_writes_frozen_workspace_artifact_chain_and_runtime_only_export" not in nodeid
        ):
            item.add_marker(pytest.mark.heavy)
        if any(
            marker in nodeid
            for marker in (
                "test_docker_executor_",
                "test_evaluator_docker_",
                "test_docker_",
            )
        ):
            item.add_marker(pytest.mark.docker)


@pytest.fixture()
def provider_local():
    return LocalDeterministicProvider()
