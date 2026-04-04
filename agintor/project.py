from __future__ import annotations

import json
import shutil
from pathlib import Path

from importlib import resources

from .benchmarks import build_demo_suite
from .pydantic_compat import model_dump
from .runtime_loader import RUNTIME_ABI_VERSION
from .runtime_sdk import bundle_runtime_kernel
from .utils import ensure_directory



def baseline_template_dir() -> Path:
    return Path(resources.files("agintor") / "templates" / "baseline_runtime")



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
