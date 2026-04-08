from __future__ import annotations

import json
import shutil
from pathlib import Path

from importlib import resources

from .benchmarks import build_demo_suite
from .pydantic_compat import model_dump
from .runtime_loader import RUNTIME_ABI_VERSION
from .runtime_profile import load_runtime_profile
from .runtime_sdk import (
    KERNEL_CAPABILITY_FLAGS,
    KERNEL_VERSION,
    STORAGE_SCHEMA_VERSION,
    bundle_runtime_kernel,
    preview_kernel_manifest,
)
from .schemas import DeploymentContract
from .utils import ensure_directory



def baseline_template_dir() -> Path:
    return Path(resources.files("agintor") / "templates" / "baseline_runtime")


def _refresh_deployment_contract(runtime_dir: Path) -> None:
    contract_path = runtime_dir / "deployment_contract.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    runtime_profile = load_runtime_profile(runtime_dir)
    kernel_manifest = preview_kernel_manifest(runtime_abi=RUNTIME_ABI_VERSION)
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
    payload["runtime_abi"] = RUNTIME_ABI_VERSION
    payload["kernel_version"] = KERNEL_VERSION
    payload["storage_schema_version"] = STORAGE_SCHEMA_VERSION
    payload["required_env_names"] = required_env_names
    payload["required_env_any_of"] = required_env_any_of
    payload["environment_allowlist"] = environment_allowlist
    payload["dependency_digest_set"] = sorted(set(kernel_manifest.files.values()))
    payload["capability_flags"] = [*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode"]
    payload["notes"] = notes
    contract = DeploymentContract(**payload)
    contract_path.write_text(json.dumps(model_dump(contract), indent=2, sort_keys=True), encoding="utf-8")



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
    bundle_runtime_kernel(dest, runtime_abi=RUNTIME_ABI_VERSION, force=True)
    _refresh_deployment_contract(dest)
    return dest



def write_demo_suite(destination: str | Path) -> Path:
    suite = build_demo_suite()
    payload = {
        "name": suite.name,
        "train": [model_dump(task) for task in suite.train],
        "val": [model_dump(task) for task in suite.val],
        "test": [model_dump(task) for task in suite.test],
        "proxy": [model_dump(task) for task in suite.proxy],
    }
    path = Path(destination)
    ensure_directory(path.parent)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
