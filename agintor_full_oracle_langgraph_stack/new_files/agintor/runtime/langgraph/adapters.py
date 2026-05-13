from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...contracts import BenchmarkTask, RuntimeSpec
from ...utils import stable_hash
from .compiler import RUNTIME_SPEC_FILE, compile_runtime_spec


class SpecBackedPolicy:
    """Compatibility policy object used by the existing LoadedRuntime shape."""

    def __init__(self, runtime_spec: RuntimeSpec | None = None) -> None:
        self.runtime_spec = runtime_spec

    def bind_runtime_spec(self, runtime_spec: RuntimeSpec) -> "SpecBackedPolicy":
        self.runtime_spec = RuntimeSpec.model_validate(runtime_spec)
        return self

    def select_mode(self, *_args, **_kwargs) -> str:
        return "langgraph"

    def retrieve(self, *_args, **_kwargs) -> list[Any]:
        return []

    def visible_tools(self, *_args, **_kwargs) -> list[str]:
        if self.runtime_spec is None:
            return []
        return [tool.tool_id for tool in self.runtime_spec.tools if tool.runtime_visible]

    def should_abort(self, *_args, **_kwargs) -> bool:
        return False


def load_runtime_spec(runtime_dir: str | Path) -> RuntimeSpec:
    runtime_dir = Path(runtime_dir)
    return RuntimeSpec.model_validate(json.loads((runtime_dir / RUNTIME_SPEC_FILE).read_text(encoding="utf-8")))


def build_spec_policy_objects(runtime_dir: str | Path) -> dict[str, SpecBackedPolicy]:
    spec = load_runtime_spec(runtime_dir)
    return {name: SpecBackedPolicy(spec) for name in ["top", "mem", "tool", "ctl"]}


def run_spec_task(runtime_dir: str | Path, task: BenchmarkTask, *, request_id: str, seed: int = 0, provider: Any | None = None, runtime_hash: str = "") -> dict[str, Any]:
    spec = load_runtime_spec(runtime_dir)
    app = compile_runtime_spec(spec, provider=provider)
    state = app.invoke(task.prompt, request_id=request_id, task_id=task.task_id, seed=seed, runtime_hash=runtime_hash)
    artifact = state.artifacts.get("answer")
    if isinstance(artifact, dict) and "answer" in artifact:
        artifact = artifact["answer"]
    return {
        "request_id": request_id,
        "task_id": task.task_id,
        "seed": seed,
        "artifact": artifact,
        "trace": state.trace,
        "side_effect_receipts": state.side_effect_receipts,
        "status": state.status,
        "runtime_spec_digest": spec.spec_digest,
        "run_digest": stable_hash(request_id, task.task_id, seed, state.artifacts, state.trace),
    }


__all__ = ["SpecBackedPolicy", "build_spec_policy_objects", "load_runtime_spec", "run_spec_task"]
