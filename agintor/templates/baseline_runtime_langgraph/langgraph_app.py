from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agintor.contracts import validate_runtime_spec_payload
from agintor.runtime.langgraph.executor import compile_runtime_spec


def load_app(runtime_dir: str | Path, provider: Any | None = None):
    runtime_path = Path(runtime_dir)
    spec = validate_runtime_spec_payload(json.loads((runtime_path / "runtime_spec.json").read_text(encoding="utf-8")))
    return compile_runtime_spec(spec, provider=provider)
