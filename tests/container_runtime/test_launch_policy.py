from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor
from agintor.core.exceptions import RuntimeLoadError
from agintor.runtime.project import init_runtime
from agintor.runtime.loader import load_runtime, resolve_docker_launch_policy
from agintor.contracts import InspectRequest
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION


def _deployment_contract_payload(
    *,
    network_policy: str = "provider-only",
    runtime_isolation_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "entry_command": "agintor solve <runtime_dir> --prompt \"<request>\"",
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "python_version": ">=3.12",
        "supported_backends": ["local", "docker"],
        "required_env_names": [],
        "environment_allowlist": [],
        "network_policy": network_policy,
        "filesystem_policy": "workspace-read-write",
        "runtime_isolation_policy": runtime_isolation_policy,
        "notes": [],
    }


def _write_deployment_contract(runtime_dir: Path, payload: dict[str, object]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "deployment_contract.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
def test_docker_executor_default_repo_root_points_at_checkout(tmp_path: Path) -> None:
    executor = DockerRuntimeExecutor(tmp_path / "host")

    assert (executor.repo_root / "pyproject.toml").is_file()
    assert (executor.repo_root / "agintor").is_dir()
    assert (executor.repo_root / "agintor/runtime/host/backends/docker/executor.py").is_file()
def test_resolve_docker_launch_policy_requires_network_none_for_restricted_contract(tmp_path: Path):
    runtime_dir = tmp_path / "restricted-runtime"
    _write_deployment_contract(runtime_dir, _deployment_contract_payload(network_policy="restricted"))

    launch_policy = resolve_docker_launch_policy(runtime_dir)

    assert launch_policy.network_none is True


def test_resolve_docker_launch_policy_allows_valid_provider_only_contract(tmp_path: Path):
    runtime_dir = tmp_path / "provider-runtime"
    _write_deployment_contract(runtime_dir, _deployment_contract_payload())

    launch_policy = resolve_docker_launch_policy(runtime_dir)

    assert launch_policy.network_none is False


def test_resolve_docker_launch_policy_raises_for_missing_contract(tmp_path: Path):
    runtime_dir = tmp_path / "missing-runtime"
    runtime_dir.mkdir(parents=True)

    with pytest.raises(RuntimeLoadError, match="missing deployment_contract.json"):
        resolve_docker_launch_policy(runtime_dir)


def test_resolve_docker_launch_policy_raises_for_corrupt_contract(tmp_path: Path):
    runtime_dir = tmp_path / "corrupt-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "deployment_contract.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="invalid JSON"):
        resolve_docker_launch_policy(runtime_dir)


def test_resolve_docker_launch_policy_raises_for_schema_invalid_contract(tmp_path: Path):
    runtime_dir = tmp_path / "invalid-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "deployment_contract.json").write_text(
        json.dumps({"network_policy": "restricted"}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeLoadError, match="invalid deployment contract schema"):
        resolve_docker_launch_policy(runtime_dir)


def test_load_runtime_rejects_local_backend_for_restricted_network_policy(tmp_path: Path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    contract_path = runtime_dir / "deployment_contract.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    payload["network_policy"] = "restricted"
    payload["runtime_isolation_policy"]["network_policy"] = "restricted"
    payload["runtime_isolation_policy"]["required_guarantees"] = []
    contract_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="cannot satisfy network policy"):
        load_runtime(runtime_dir, runtime_backend="local")


def test_inspect_fails_closed_before_any_subprocess_launch_on_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime_dir = tmp_path / "corrupt-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "deployment_contract.json").write_text("{not json", encoding="utf-8")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _unexpected_run(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run should not be called when launch policy resolution fails")

    monkeypatch.setattr("agintor.runtime.host.backends.docker.executor.subprocess.run", _unexpected_run)

    executor = DockerRuntimeExecutor(tmp_path / "executor")
    request = InspectRequest(
        request_id="inspect.1",
        requested_backend="docker",
        expected_runtime_contract_version=RUNTIME_CONTRACT_VERSION,
    )

    with pytest.raises(RuntimeLoadError, match="invalid JSON"):
        executor.inspect(runtime_dir, request)

    assert calls == []
