from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from agintor.factory.goals import build_goal_spec
from agintor.factory.planning import (
    _build_benchmark_plan,
    _build_runtime_plan,
    build_goal_conditioned_suite,
)
from agintor.runtime import profile as profile_module
from agintor.runtime.profile import default_runtime_profile
from agintor.runtime.sdk import bundle_runtime_kernel, validate_kernel_bundle


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_profile_uses_only_the_canonical_packaged_resource(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_root = tmp_path / "installed" / "agintor"
    canonical = package_root / "runtime" / "sdk" / "defaults" / "runtime_profile.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(
        (REPO_ROOT / "agintor" / "runtime" / "sdk" / "defaults" / "runtime_profile.json").read_bytes()
    )
    monkeypatch.setattr(profile_module.resources, "files", lambda _package: package_root)

    profile = default_runtime_profile()

    assert profile.runtime_provider.name == "openai"
    assert not (package_root / "templates" / "baseline_runtime").exists()


def test_minimal_sdk_fixture_imports_with_checkout_hidden_and_validates_files(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "fixture"
    manifest = bundle_runtime_kernel(runtime_dir, force=True)
    bundle_root = (runtime_dir / "runtime_sdk").resolve()
    expected_files = {
        "agintor_runtime/contracts/epochs.py",
        "agintor_runtime/contracts/harness.py",
        "agintor_runtime/core/identity.py",
        "agintor_runtime/runtime/kernel/composite_runtime.py",
        "agintor_runtime/runtime/kernel/composite_replay_provider.py",
        "agintor_runtime/runtime/sdk/harness_entrypoint.py",
    }

    assert expected_files <= set(manifest.files)
    assert validate_kernel_bundle(runtime_dir).model_dump() == manifest.model_dump()

    script = f"""
import json
import sys
sys.path.insert(0, {str(bundle_root)!r})
import agintor_runtime
from agintor_runtime.contracts.epochs import TaskEnvelope
from agintor_runtime.core.identity import task_digest
from agintor_runtime.runtime.harness_profile import HarnessDeploymentProfile
from agintor_runtime.runtime.sdk.harness_entrypoint import HarnessSolveFileRequest
from agintor_runtime import runtime_entry

print(json.dumps({{
    "package_file": agintor_runtime.__file__,
    "entry_file": runtime_entry.__file__,
    "task_digest": task_digest({{"task": "offline-smoke"}}),
    "task": TaskEnvelope.__name__,
    "solve_request": HarnessSolveFileRequest.__name__,
    "entry_module": runtime_entry._main.__module__,
    "profile": HarnessDeploymentProfile.__name__,
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert Path(payload["package_file"]).is_relative_to(bundle_root)
    assert Path(payload["entry_file"]).is_relative_to(bundle_root)
    assert payload["task"] == "TaskEnvelope"
    assert payload["solve_request"] == "HarnessSolveFileRequest"
    assert payload["entry_module"].endswith("harness_entrypoint")
    assert payload["profile"] == "HarnessDeploymentProfile"

    epochs_file = runtime_dir / "runtime_sdk" / "agintor_runtime" / "contracts" / "epochs.py"
    epochs_file.write_text(epochs_file.read_text(encoding="utf-8") + "\n# mismatch\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_kernel_bundle(runtime_dir)

    bundle_runtime_kernel(runtime_dir, force=True)
    epochs_file.unlink()
    with pytest.raises(FileNotFoundError, match="file is missing"):
        validate_kernel_bundle(runtime_dir)


def test_package_metadata_has_a_real_readme_and_no_ignored_baseline_resource() -> None:
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = REPO_ROOT / metadata["project"]["readme"]
    package_data = metadata["tool"]["setuptools"]["package-data"]["agintor"]

    assert readme.is_file()
    assert readme.read_text(encoding="utf-8").startswith("# Agintor")
    assert "runtime/sdk/defaults/*.json" in package_data
    assert all("templates/baseline_runtime/" not in item for item in package_data)


def test_spec_planning_does_not_load_the_absent_policy_template(monkeypatch) -> None:
    import agintor.factory.export as export_module
    import agintor.runtime.project as project_module

    def unexpected_legacy_access():
        raise AssertionError("spec planning accessed the legacy policy template")

    monkeypatch.setattr(export_module, "_load_template_manifest", unexpected_legacy_access)
    monkeypatch.setattr(project_module, "baseline_template_dir", unexpected_legacy_access)
    profile = default_runtime_profile()
    goal = build_goal_spec("repair a Python repository", runtime_provider_name="openai")
    goal = goal.model_copy(
        update={"constraints": {**goal.constraints, "runtime_kind": "langgraph_spec"}}
    )
    suite = build_goal_conditioned_suite(goal, profile)
    benchmark_plan = _build_benchmark_plan(goal, suite)

    runtime_plan = _build_runtime_plan(
        goal,
        suite,
        benchmark_plan,
        profile,
        agintor_provider="local",
        runtime_backend="local",
    )

    assert runtime_plan.runtime_kind == "langgraph_spec"
    assert runtime_plan.seed_template == ""
    assert runtime_plan.mutable_files == []
    assert runtime_plan.immutable_manifest == []
