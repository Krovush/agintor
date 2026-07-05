from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...contracts import BenchmarkTask, RuntimeSpec, validate_runtime_spec_payload
from ...utils import stable_hash
from .executor import RUNTIME_SPEC_FILE, compile_runtime_spec


class SpecBackedPolicy:
    """Compatibility policy object used by the existing LoadedRuntime shape."""

    def __init__(self, runtime_spec: RuntimeSpec | None = None) -> None:
        self.runtime_spec = validate_runtime_spec_payload(runtime_spec) if runtime_spec is not None else None

    def bind_runtime_spec(self, runtime_spec: RuntimeSpec) -> "SpecBackedPolicy":
        self.runtime_spec = validate_runtime_spec_payload(runtime_spec)
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
    return validate_runtime_spec_payload(json.loads((runtime_dir / RUNTIME_SPEC_FILE).read_text(encoding="utf-8")))


_MISSING = object()


def _unwrap_answer_artifact(artifact: Any) -> Any:
    if isinstance(artifact, dict) and "answer" in artifact:
        return artifact["answer"]
    return artifact


def _artifact_from_keys(artifacts: Mapping[str, Any], keys: list[str]) -> Any:
    values = [(key, _unwrap_answer_artifact(artifacts[key])) for key in keys if key in artifacts]
    if not values:
        return _MISSING
    if len(values) == 1:
        return values[0][1]
    return {key: value for key, value in values}


def resolve_output_artifact(spec: RuntimeSpec, artifacts: Mapping[str, Any]) -> Any:
    node_by_id = {node.node_id: node for node in spec.graph.nodes}
    for terminal_id in spec.graph.terminal_nodes:
        terminal = node_by_id.get(terminal_id)
        if terminal is None:
            continue
        input_artifact = _artifact_from_keys(artifacts, list(terminal.input_keys))
        if input_artifact is not _MISSING:
            return input_artifact
        if terminal.output_key and terminal.output_key in artifacts:
            return _unwrap_answer_artifact(artifacts[terminal.output_key])
    if spec.runtime_kind == "langgraph_spec" and "answer" in artifacts:
        return _unwrap_answer_artifact(artifacts["answer"])
    return None


def build_spec_policy_objects(runtime_dir: str | Path) -> dict[str, SpecBackedPolicy]:
    spec = load_runtime_spec(runtime_dir)
    return {name: SpecBackedPolicy(spec) for name in ["top", "mem", "tool", "ctl"]}


def run_spec_task(
    runtime_dir: str | Path,
    task: BenchmarkTask,
    *,
    request_id: str,
    seed: int = 0,
    provider: Any | None = None,
    runtime_hash: str = "",
    trace_context: Any | None = None,
) -> dict[str, Any]:
    spec = load_runtime_spec(runtime_dir)
    app = compile_runtime_spec(spec, provider=provider)
    state = app.invoke(task.prompt, request_id=request_id, task_id=task.task_id, seed=seed, runtime_hash=runtime_hash, trace_context=trace_context)
    artifact = resolve_output_artifact(spec, state.artifacts)
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


__all__ = [
    "SpecBackedPolicy",
    "build_spec_policy_objects",
    "load_runtime_spec",
    "resolve_output_artifact",
    "run_spec_task",
]
