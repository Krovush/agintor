from __future__ import annotations

import json
import shutil
from pathlib import Path
from textwrap import dedent
from typing import Any

from ...contracts import DeploymentContract, RuntimeIsolationPolicy, RuntimeManifest, RuntimeSpec, validate_runtime_spec_payload
from ...core.versioning import RUNTIME_CONTRACT_VERSION
from ...runtime.sdk import bundle_runtime_kernel
from ...utils import ensure_directory
from .executor import GENERATED_APP_FILE, RUNTIME_SPEC_FILE, runtime_spec_code_hash, validate_pass1_supported_subset


class RuntimeSpecCompiler:
    """Factory/export compiler for spec-backed runtimes."""

    def compile_to_directory(self, runtime_spec: RuntimeSpec | dict[str, Any], destination: str | Path, *, force: bool = False) -> Path:
        spec = validate_pass1_supported_subset(validate_runtime_spec_payload(runtime_spec))
        destination = Path(destination)
        if destination.exists():
            if force:
                shutil.rmtree(destination)
            elif not destination.is_dir():
                raise FileExistsError(f"runtime destination exists and is not a directory: {destination}")
            elif any(destination.iterdir()):
                raise FileExistsError(f"runtime destination is not empty: {destination}; pass force=True to replace it")
        ensure_directory(destination)
        self._write_spec(spec, destination)
        self._write_generated_app(destination)
        self._write_manifest(spec, destination)
        self._write_deployment_contract(spec, destination)
        bundle_runtime_kernel(destination, force=True, profile="legacy")
        return destination

    @staticmethod
    def _write_spec(spec: RuntimeSpec, destination: Path) -> None:
        (destination / RUNTIME_SPEC_FILE).write_text(
            json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _write_generated_app(destination: Path) -> None:
        source = f"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agintor.contracts import validate_runtime_spec_payload
from agintor.runtime.langgraph.executor import RUNTIME_SPEC_FILE, compile_runtime_spec


def load_app(runtime_dir: str | Path, provider: Any | None = None):
    runtime_dir = Path(runtime_dir)
    spec = validate_runtime_spec_payload(json.loads((runtime_dir / RUNTIME_SPEC_FILE).read_text(encoding='utf-8')))
    return compile_runtime_spec(spec, provider=provider)


def invoke(runtime_dir: str | Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
    app = load_app(runtime_dir, provider=kwargs.pop('provider', None))
    state = app.invoke(prompt, **kwargs)
    return state.model_dump(mode='json', exclude_none=True)
"""
        (destination / GENERATED_APP_FILE).write_text(dedent(source).lstrip(), encoding="utf-8")

    @staticmethod
    def _write_manifest(spec: RuntimeSpec, destination: Path) -> None:
        manifest = RuntimeManifest(
            runtime_id=spec.runtime_id,
            version="2",
            policy_modules={
                "top": f"{GENERATED_APP_FILE}:SpecBackedPolicy",
                "mem": f"{GENERATED_APP_FILE}:SpecBackedPolicy",
                "tool": f"{GENERATED_APP_FILE}:SpecBackedPolicy",
                "ctl": f"{GENERATED_APP_FILE}:SpecBackedPolicy",
            },
            mutable_files=[RUNTIME_SPEC_FILE],
            immutable_manifest=[GENERATED_APP_FILE],
            runtime_kind=spec.runtime_kind,
            runtime_spec_path=RUNTIME_SPEC_FILE,
            runtime_spec_digest=spec.spec_digest,
            metadata={
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "runtime_kind": spec.runtime_kind,
                "runtime_spec_ref": RUNTIME_SPEC_FILE,
                "runtime_spec_digest": spec.spec_digest,
            },
        )
        (destination / "runtime_manifest.json").write_text(
            json.dumps(manifest.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _write_deployment_contract(spec: RuntimeSpec, destination: Path) -> None:
        network_policy = "open" if any(tool.side_effect_kind == "service_action" for tool in spec.tools) else "provider-only"
        contract = DeploymentContract(
            entry_command="python -m agintor_runtime.entrypoint",
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
            python_version=">=3.12",
            supported_backends=["local", "docker"],
            network_policy=network_policy,
            filesystem_policy="workspace-read-write",
            capability_flags=["runtime_spec", "langgraph_spec", "side_effect_receipts"],
            runtime_isolation_policy=RuntimeIsolationPolicy(
                timeout_envelope={},
                workspace_root=".",
                network_policy=network_policy,
                filesystem_policy="workspace-read-write",
                required_guarantees=["timeout_enforcement", "workspace_isolation", "environment_filtering"],
            ),
            notes=["Spec-backed generated LangGraph runtime."],
        )
        (destination / "deployment_contract.json").write_text(
            json.dumps(contract.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True),
            encoding="utf-8",
        )


__all__ = ["RuntimeSpecCompiler", "runtime_spec_code_hash"]
