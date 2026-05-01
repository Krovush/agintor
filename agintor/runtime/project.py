from __future__ import annotations

import json
import shutil
from pathlib import Path

from importlib import resources

from ..evaluation.benchmarks import build_demo_suite
from .profile import load_runtime_profile
from .sdk import (
    KERNEL_CAPABILITY_FLAGS,
    bundle_runtime_kernel,
    preview_kernel_manifest,
)
from ..contracts import DeploymentContract
from ..utils import ensure_directory
from ..core.versioning import RUNTIME_CONTRACT_VERSION



def baseline_template_dir() -> Path:
    return Path(resources.files("agintor") / "templates" / "baseline_runtime")


def _refresh_deployment_contract(runtime_dir: Path) -> None:
    contract_path = runtime_dir / "deployment_contract.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    runtime_profile = load_runtime_profile(runtime_dir)
    kernel_manifest = preview_kernel_manifest()
    required_env_names = []
    required_env_any_of: list[list[str]] = []
    api_key_env = str(runtime_profile.runtime_provider.api_key_env or "").strip()
    api_key_file_env = str(runtime_profile.runtime_provider.api_key_file_env or "").strip()
    credential_group = [name for name in [api_key_env, api_key_file_env] if name]
    if credential_group:
        required_env_any_of.append(credential_group)
    environment_allowlist = sorted(
        {
            *required_env_names,
            *credential_group,
            str(runtime_profile.runtime_provider.base_url_env or "").strip(),
            str(runtime_profile.runtime_provider.pricing_env or "").strip(),
        }
    )
    environment_allowlist = [name for name in environment_allowlist if name]
    notes = [str(note) for note in payload.get("notes", []) if str(note).strip()]
    if api_key_file_env:
        note = f"{api_key_file_env} may be used as a key-file alternative for the default runtime provider."
        if note not in notes:
            notes.append(note)
    payload["runtime_contract_version"] = RUNTIME_CONTRACT_VERSION
    payload["required_env_names"] = required_env_names
    payload["required_env_any_of"] = required_env_any_of
    payload["environment_allowlist"] = environment_allowlist
    payload["dependency_digest_set"] = sorted(set(kernel_manifest.files.values()))
    payload["capability_flags"] = [*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode"]
    payload["runtime_isolation_policy"] = {
        "timeout_envelope": {"seconds": runtime_profile.execution.latency_max},
        "workspace_root": ".",
        "environment_allowlist": environment_allowlist,
        "network_policy": str(payload.get("network_policy", "provider-only")),
        "filesystem_policy": str(payload.get("filesystem_policy", "workspace-read-write")),
        "required_guarantees": [
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
        ],
        "desired_guarantees": ["process_cleanup"],
    }
    if str(payload.get("network_policy", "provider-only")) in {"none", "restricted"}:
        payload["runtime_isolation_policy"]["required_guarantees"].append("network_disablement")
    payload["notes"] = notes
    contract = DeploymentContract(**payload)
    contract_path.write_text(json.dumps((contract).model_dump(), indent=2, sort_keys=True), encoding="utf-8")


def _refresh_runtime_manifest(runtime_dir: Path) -> None:
    manifest_path = runtime_dir / "runtime_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    payload["metadata"] = {
        **metadata,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")



def init_runtime(destination: str | Path, force: bool = False) -> Path:
    dest = Path(destination)
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FileExistsError(f"destination {dest} is not empty")
    if dest.exists() and force:
        shutil.rmtree(dest)
    ensure_directory(dest.parent)
    template_root = resources.files("agintor").joinpath("templates", "baseline_runtime")
    with resources.as_file(template_root) as template_dir:
        shutil.copytree(template_dir, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    bundle_runtime_kernel(dest, force=True)
    _refresh_runtime_manifest(dest)
    _refresh_deployment_contract(dest)
    return dest



def write_demo_suite(destination: str | Path) -> Path:
    suite = build_demo_suite()
    payload = {
        "name": suite.name,
        "train": [(task).model_dump() for task in suite.train],
        "val": [(task).model_dump() for task in suite.val],
        "test": [(task).model_dump() for task in suite.test],
        "proxy": [(task).model_dump() for task in suite.proxy],
    }
    path = Path(destination)
    ensure_directory(path.parent)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
