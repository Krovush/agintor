from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

from ...contracts import DeploymentContract, RuntimeIsolationPolicy, RuntimeManifest, RuntimeSpec
from ...runtime.sdk import bundle_runtime_kernel
from ...utils import ensure_directory, stable_hash
from ...core.versioning import RUNTIME_CONTRACT_VERSION
from .operation_service import RuntimeOperationService
from .state import LangGraphRuntimeState

RUNTIME_SPEC_FILE = "runtime_spec.json"
GENERATED_APP_FILE = "generated_langgraph_app.py"


class CompiledSpecRuntime:
    def __init__(self, runtime_spec: RuntimeSpec, *, provider: Any | None = None) -> None:
        self.runtime_spec = RuntimeSpec.model_validate(runtime_spec)
        self.service = RuntimeOperationService(self.runtime_spec, provider=provider)

    def invoke(self, prompt: str, *, request_id: str = "", task_id: str = "", seed: int = 0, runtime_hash: str = "") -> LangGraphRuntimeState:
        state = LangGraphRuntimeState(
            request_id=request_id,
            task_id=task_id,
            seed=seed,
            prompt=prompt,
            runtime_hash=runtime_hash,
            runtime_spec_digest=self.runtime_spec.spec_digest,
            budget={"model_calls": 0, "tool_calls": 0},
        )
        node_by_id = {node.node_id: node for node in self.runtime_spec.graph.nodes}
        current = self.runtime_spec.graph.entry_node
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            node = node_by_id[current]
            result = self.service.run_node(state, node)
            if result.status == "failed":
                break
            if current in set(self.runtime_spec.graph.terminal_nodes):
                state.status = "completed"
                break
            outgoing = [edge for edge in self.runtime_spec.graph.edges if edge.source == current]
            if not outgoing:
                state.status = "completed"
                break
            outgoing.sort(key=lambda edge: edge.priority)
            current = outgoing[0].target
        if state.status == "running":
            state.status = "completed"
        return state


def compile_runtime_spec(runtime_spec: RuntimeSpec | dict[str, Any], *, provider: Any | None = None) -> CompiledSpecRuntime:
    return CompiledSpecRuntime(RuntimeSpec.model_validate(runtime_spec), provider=provider)


class RuntimeSpecCompiler:
    def compile_to_directory(self, runtime_spec: RuntimeSpec | dict[str, Any], destination: str | Path, *, force: bool = False) -> Path:
        spec = RuntimeSpec.model_validate(runtime_spec)
        destination = Path(destination)
        if destination.exists() and force:
            import shutil
            shutil.rmtree(destination)
        ensure_directory(destination)
        self._write_spec(spec, destination)
        self._write_generated_app(spec, destination)
        self._write_manifest(spec, destination)
        self._write_deployment_contract(spec, destination)
        bundle_runtime_kernel(destination, force=True)
        return destination

    @staticmethod
    def _write_spec(spec: RuntimeSpec, destination: Path) -> None:
        (destination / RUNTIME_SPEC_FILE).write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _write_generated_app(spec: RuntimeSpec, destination: Path) -> None:
        source = f"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agintor.contracts import RuntimeSpec
from agintor.runtime.langgraph.compiler import compile_runtime_spec


def load_app(runtime_dir: str | Path, provider: Any | None = None):
    runtime_dir = Path(runtime_dir)
    spec = RuntimeSpec.model_validate(json.loads((runtime_dir / {RUNTIME_SPEC_FILE!r}).read_text(encoding='utf-8')))
    return compile_runtime_spec(spec, provider=provider)


def invoke(runtime_dir: str | Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
    app = load_app(runtime_dir, provider=kwargs.pop('provider', None))
    state = app.invoke(prompt, **kwargs)
    return state.model_dump(mode='json')
"""
        (destination / GENERATED_APP_FILE).write_text(dedent(source).lstrip(), encoding="utf-8")

    @staticmethod
    def _write_manifest(spec: RuntimeSpec, destination: Path) -> None:
        manifest = RuntimeManifest(
            runtime_id=spec.runtime_id,
            version="2",
            policy_modules={"top": f"{GENERATED_APP_FILE}:SpecBackedPolicy", "mem": f"{GENERATED_APP_FILE}:SpecBackedPolicy", "tool": f"{GENERATED_APP_FILE}:SpecBackedPolicy", "ctl": f"{GENERATED_APP_FILE}:SpecBackedPolicy"},
            mutable_files=[RUNTIME_SPEC_FILE],
            immutable_manifest=[GENERATED_APP_FILE, RUNTIME_SPEC_FILE],
            metadata={
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "runtime_kind": spec.runtime_kind,
                "runtime_spec_ref": RUNTIME_SPEC_FILE,
                "runtime_spec_digest": spec.spec_digest,
            },
        )
        payload = manifest.model_dump(mode="json", exclude_none=True)
        (destination / "runtime_manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _write_deployment_contract(spec: RuntimeSpec, destination: Path) -> None:
        contract = DeploymentContract(
            entry_command="python -m agintor_runtime.entrypoint",
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
            python_version=">=3.12",
            supported_backends=["local", "docker"],
            network_policy="open" if any(tool.side_effect_kind == "service_action" for tool in spec.tools) else "none",
            filesystem_policy="workspace-read-write",
            capability_flags=["runtime_spec_v2", "langgraph_spec_v2", "side_effect_receipts"],
            runtime_isolation_policy=RuntimeIsolationPolicy(
                workspace_root=".",
                network_policy="open" if any(tool.side_effect_kind == "service_action" for tool in spec.tools) else "none",
                filesystem_policy="workspace-read-write",
                required_guarantees=["timeout_enforcement", "workspace_isolation", "environment_filtering"],
            ),
            notes=["Spec-backed generated LangGraph/LangChain runtime."],
        )
        (destination / "deployment_contract.json").write_text(json.dumps(contract.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True), encoding="utf-8")


def runtime_spec_code_hash(spec: RuntimeSpec) -> str:
    return stable_hash("langgraph_spec_v2", spec.spec_digest, RUNTIME_CONTRACT_VERSION)


__all__ = ["CompiledSpecRuntime", "GENERATED_APP_FILE", "RUNTIME_SPEC_FILE", "RuntimeSpecCompiler", "compile_runtime_spec", "runtime_spec_code_hash"]
