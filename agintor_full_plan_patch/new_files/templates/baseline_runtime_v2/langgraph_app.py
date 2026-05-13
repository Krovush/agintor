from __future__ import annotations

import json
from pathlib import Path

from agintor.contracts import RuntimeSpec
from agintor.runtime.langgraph.compiler import LangGraphRuntimeCompiler


def load_app(runtime_dir: str | Path):
    runtime_path = Path(runtime_dir)
    spec = RuntimeSpec.model_validate(json.loads((runtime_path / "runtime_spec.json").read_text(encoding="utf-8")))
    return LangGraphRuntimeCompiler().compile(spec)
