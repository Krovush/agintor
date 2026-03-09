from __future__ import annotations

import json
import shutil
from pathlib import Path

from importlib import resources

from .benchmarks import build_demo_suite
from .pydantic_compat import model_dump
from .utils import ensure_directory



def baseline_template_dir() -> Path:
    return Path(resources.files("agintor") / "templates" / "baseline_runtime")



def init_runtime(destination: str | Path, force: bool = False) -> Path:
    dest = Path(destination)
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FileExistsError(f"destination {dest} is not empty")
    if dest.exists() and force:
        shutil.rmtree(dest)
    shutil.copytree(baseline_template_dir(), dest)
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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
