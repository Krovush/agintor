from __future__ import annotations

import importlib
import json
from pathlib import Path
import shutil
import sys

import agintor.runtime.project as project
import agintor.runtime.sdk.bundle as runtime_bundle
from agintor.providers import build_provider
from agintor.runtime.api import load_solve_request, runtime_solve_request_for_user_request
from agintor.runtime.host import RuntimeHost
from agintor.runtime.loader import load_runtime
from agintor.runtime.sdk import bundle_runtime_kernel, preview_kernel_manifest
from agintor.runtime.profile import RUNTIME_PROFILE_FILE, load_runtime_profile
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION


_FORBIDDEN_BUNDLE_PREFIXES = (
    "agintor_runtime/evaluation/",
    "agintor_runtime/factory/",
    "agintor_runtime/learning/",
    "agintor_runtime/runtime/host/",
    "agintor_runtime/search/",
)
_FORBIDDEN_BUNDLE_FILES = {
    "agintor_runtime/cli.py",
    "agintor_runtime/contracts/factory.py",
    "agintor_runtime/contracts/search.py",
    "agintor_runtime/core/patches.py",
    "agintor_runtime/runtime/project.py",
    "agintor_runtime/runtime/sdk/bundle.py",
    "agintor_runtime/storage/factory_chat_store.py",
    "agintor_runtime/storage/runtime_session_store.py",
    "agintor_runtime/templates/baseline_runtime/control_policy.py",
    "agintor_runtime/templates/baseline_runtime/memory_policy.py",
    "agintor_runtime/templates/baseline_runtime/tool_policy.py",
    "agintor_runtime/templates/baseline_runtime/topology_policy.py",
}


def _forbidden_bundle_files(file_names) -> list[str]:
    return sorted(
        file_name
        for file_name in file_names
        if file_name in _FORBIDDEN_BUNDLE_FILES
        or any(file_name.startswith(prefix) for prefix in _FORBIDDEN_BUNDLE_PREFIXES)
    )


def _copy_package_source_for_manifest_test(tmp_path: Path) -> Path:
    copied_root = tmp_path / "agintor-source"
    shutil.copytree(
        runtime_bundle._package_root(),
        copied_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return copied_root


def _write_local_runtime_profile(runtime_dir: Path) -> None:
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"runtime_provider": {"name": "local"}}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    project._refresh_deployment_contract(runtime_dir)


def test_load_runtime_profile_accepts_legacy_provider_key_from_runtime_dir(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "local"}}, indent=2),
        encoding="utf-8",
    )

    profile = load_runtime_profile(runtime_dir)

    assert profile.runtime_provider.name == "local"
    assert profile.runtime_provider.api_key_env is None
    assert profile.runtime_provider.api_key_file_env is None
    assert profile.runtime_provider.base_url_env is None
    assert profile.runtime_provider.pricing_env is None
    assert profile.runtime_provider.model_map == {}
    assert profile.runtime_provider.reasoning_effort_map == {}
    assert profile.runtime_provider.pricing_map == {}

def test_load_runtime_profile_uses_minimax_defaults_for_legacy_provider_key(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "minimax"}}, indent=2),
        encoding="utf-8",
    )

    profile = load_runtime_profile(runtime_dir)

    assert profile.runtime_provider.name == "minimax"
    assert profile.runtime_provider.api_key_env == "AGINTOR_MAS_MINIMAX_API_KEY"
    assert profile.runtime_provider.api_key_file_env == "AGINTOR_MAS_MINIMAX_KEY_FILE"
    assert profile.runtime_provider.base_url is None
    assert profile.runtime_provider.base_url_env == "AGINTOR_MAS_MINIMAX_BASE_URL"
    assert profile.runtime_provider.pricing_env == "AGINTOR_MAS_MINIMAX_PRICING"
    assert profile.runtime_provider.model_map == {
        "small": "MiniMax-M2.7-Flash",
        "medium": "MiniMax-M2.7-Flash",
        "large": "MiniMax-M2.7-Flash",
    }
    assert not any(
        "OPENAI" in str(value)
        for value in [
            profile.runtime_provider.api_key_env,
            profile.runtime_provider.api_key_file_env,
            profile.runtime_provider.base_url_env,
            profile.runtime_provider.pricing_env,
            *profile.runtime_provider.model_map.values(),
        ]
    )

def test_load_runtime_profile_prefers_runtime_provider_over_legacy_provider(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "provider": {"name": "local"},
                "runtime_provider": {
                    "name": "minimax",
                    "api_key_env": "EXPLICIT_MINIMAX_KEY",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    profile = load_runtime_profile(profile_path=profile_path)

    assert profile.runtime_provider.name == "minimax"
    assert profile.runtime_provider.api_key_env == "EXPLICIT_MINIMAX_KEY"
    assert profile.runtime_provider.api_key_file_env == "AGINTOR_MAS_MINIMAX_KEY_FILE"
    assert profile.runtime_provider.base_url is None
    assert profile.runtime_provider.base_url_env == "AGINTOR_MAS_MINIMAX_BASE_URL"
    assert profile.runtime_provider.pricing_env == "AGINTOR_MAS_MINIMAX_PRICING"
    assert "OPENAI" not in str(profile.runtime_provider.model_map)

def test_legacy_minimax_profile_allows_base_url_env_override(tmp_path: Path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "minimax"}}, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGINTOR_MAS_MINIMAX_BASE_URL", "https://minimax.example.test/anthropic")

    profile = load_runtime_profile(runtime_dir)
    provider = build_provider(profile.runtime_provider.name, provider_profile=profile.runtime_provider)

    assert profile.runtime_provider.base_url is None
    assert provider.base_url == "https://minimax.example.test/anthropic"

def test_init_runtime_refreshes_runtime_manifest_contract_version(monkeypatch, tmp_path: Path):
    current_contract_version = f"{RUNTIME_CONTRACT_VERSION}.test"
    monkeypatch.setattr(project, "RUNTIME_CONTRACT_VERSION", current_contract_version)

    runtime_dir = project.init_runtime(tmp_path / "runtime")

    manifest = json.loads((runtime_dir / "runtime_manifest.json").read_text(encoding="utf-8"))
    contract = json.loads((runtime_dir / "deployment_contract.json").read_text(encoding="utf-8"))

    assert manifest["metadata"]["runtime_contract_version"] == current_contract_version
    assert contract["runtime_contract_version"] == current_contract_version

def test_refresh_deployment_contract_does_not_require_openai_credentials_for_legacy_local_profile(tmp_path: Path):
    runtime_dir = project.init_runtime(tmp_path / "runtime")
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "local"}}, indent=2),
        encoding="utf-8",
    )

    project._refresh_deployment_contract(runtime_dir)

    contract = json.loads((runtime_dir / "deployment_contract.json").read_text(encoding="utf-8"))
    assert contract["required_env_any_of"] == []
    assert "OPENAI_API_KEY" not in contract["environment_allowlist"]
    assert "AGINTOR_OPENAI_KEY_FILE" not in contract["environment_allowlist"]

def test_task_runtime_facade_is_exported_in_bundled_kernel(tmp_path: Path):
    from agintor.runtime.kernel.facade import TaskRuntime as HostTaskRuntime

    runtime_dir = tmp_path / "runtime"
    manifest = bundle_runtime_kernel(runtime_dir, force=True)
    sdk_path = str((runtime_dir / "runtime_sdk").resolve())

    assert HostTaskRuntime.__name__ == "TaskRuntime"
    assert hasattr(HostTaskRuntime, "run_task")
    assert hasattr(HostTaskRuntime, "resume_from_checkpoint")
    assert hasattr(HostTaskRuntime, "_run_branch_plan")
    assert hasattr(HostTaskRuntime, "_execute_isolated_frame")
    assert "agintor_runtime/runtime/kernel/base.py" in manifest.files
    assert "agintor_runtime/runtime/kernel/branches/execution.py" in manifest.files
    assert "agintor_runtime/runtime/kernel/local_verifiers.py" in manifest.files
    assert "agintor_runtime/runtime/kernel/predictors.py" in manifest.files

    for module_name in list(sys.modules):
        if module_name == "agintor_runtime" or module_name.startswith("agintor_runtime."):
            del sys.modules[module_name]
    sys.path.insert(0, sdk_path)
    try:
        bundled_runner = importlib.import_module("agintor_runtime.runtime.kernel.facade")
        bundled_task_runtime = bundled_runner.TaskRuntime
        assert bundled_task_runtime.__name__ == "TaskRuntime"
        assert hasattr(bundled_task_runtime, "run_task")
        assert hasattr(bundled_task_runtime, "resume_from_checkpoint")
        assert hasattr(bundled_task_runtime, "_run_branch_plan")
        assert hasattr(bundled_task_runtime, "_execute_isolated_frame")
    finally:
        sys.path.remove(sdk_path)
        for module_name in list(sys.modules):
            if module_name == "agintor_runtime" or module_name.startswith("agintor_runtime."):
                del sys.modules[module_name]


def test_runtime_kernel_bundle_excludes_host_factory_search_learning_evaluation_and_template_python(tmp_path: Path):
    preview = preview_kernel_manifest()
    runtime_dir = project.init_runtime(tmp_path / "runtime")
    bundled_manifest = json.loads(
        (runtime_dir / "runtime_sdk" / "kernel_manifest.json").read_text(encoding="utf-8")
    )
    files = set(bundled_manifest["files"])

    assert _forbidden_bundle_files(preview.files) == []
    assert _forbidden_bundle_files(files) == []
    assert "agintor_runtime/runtime/kernel/base.py" in files
    assert "agintor_runtime/runtime/kernel/local_verifiers.py" in files
    assert "agintor_runtime/runtime/kernel/predictors.py" in files
    assert "agintor_runtime/runtime/sdk/entrypoint.py" in files
    assert "agintor_runtime/storage/run_store.py" in files
    assert "agintor_runtime/templates/prompts/memory.span_summarize.json" in files
    assert "agintor_runtime/templates/prompts/tool.spec_generate.json" in files


def test_generated_template_python_does_not_affect_preview_or_bundled_manifest(
    monkeypatch,
    tmp_path: Path,
):
    copied_root = _copy_package_source_for_manifest_test(tmp_path)
    monkeypatch.setattr(runtime_bundle, "_package_root", lambda: copied_root)

    preview_before = preview_kernel_manifest()
    bundled_before = bundle_runtime_kernel(tmp_path / "runtime-before", force=True)

    generated_template = copied_root / "templates" / "baseline_runtime" / "_generated_policy_shadow.py"
    generated_template.write_text("GENERATED_DIRTY_SENTINEL = 'ignore me'\n", encoding="utf-8")
    topology_template = copied_root / "templates" / "baseline_runtime" / "topology_policy.py"
    topology_template.write_text(
        topology_template.read_text(encoding="utf-8")
        + "\nDIRTY_SENTINEL = 'ignored by bundle manifest'\n",
        encoding="utf-8",
    )

    preview_after = preview_kernel_manifest()
    bundled_after = bundle_runtime_kernel(tmp_path / "runtime-after", force=True)

    assert _forbidden_bundle_files(preview_after.files) == []
    assert _forbidden_bundle_files(bundled_after.files) == []
    assert preview_after.model_dump() == preview_before.model_dump()
    assert bundled_after.model_dump() == bundled_before.model_dump()


def test_host_only_source_changes_do_not_change_runtime_identity(monkeypatch, tmp_path: Path):
    copied_root = _copy_package_source_for_manifest_test(tmp_path)
    monkeypatch.setattr(runtime_bundle, "_package_root", lambda: copied_root)

    first_runtime_dir = project.init_runtime(tmp_path / "runtime-before")
    first_runtime = load_runtime(first_runtime_dir, runtime_backend="local")

    host_file = copied_root / "runtime" / "host" / "host.py"
    host_file.write_text(
        host_file.read_text(encoding="utf-8")
        + "\nHOST_ONLY_SENTINEL = 'ignored by runtime identity'\n",
        encoding="utf-8",
    )
    second_runtime_dir = project.init_runtime(tmp_path / "runtime-after")
    second_runtime = load_runtime(second_runtime_dir, runtime_backend="local")

    assert second_runtime.code_hash == first_runtime.code_hash
    assert second_runtime.runtime_hash == first_runtime.runtime_hash


def test_fresh_exported_runtime_loads_and_solves_through_runtime_entrypoint(tmp_path: Path):
    runtime_dir = project.init_runtime(tmp_path / "runtime")
    _write_local_runtime_profile(runtime_dir)
    runtime_profile = load_runtime_profile(runtime_dir)
    loaded_runtime = load_runtime(runtime_dir, runtime_backend="local")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    provider = build_provider("local", provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello from a fresh exported runtime."),
    )

    response = host.solve(runtime_dir, request, provider=provider, runtime_profile=runtime_profile)

    assert response.solve_result.runtime_hash == loaded_runtime.runtime_hash
    assert response.solve_result.status in {"best_effort", "verified"}
    assert response.solve_result.faults["hard_invalid"] is False
    assert response.solve_result.run_id
    assert response.solve_result.run_root
