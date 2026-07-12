from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from agintor.factory.harness_release import publish_harness_release
from agintor.isolation.replay import (
    IsolatedCommandReplayBinding,
    IsolatedCommandReplayRecorder,
    write_isolated_command_replay_manifest,
)
from agintor.runtime.api.composite_compiler import compile_composite_run_plan
from agintor.runtime.kernel.composite_provider import ProviderInvocation
from agintor.runtime.kernel.composite_replay_provider import (
    CompositeReplayBinding,
    CompositeReplayRecorder,
    write_composite_replay_manifest,
)
from agintor.runtime.sdk.bundle import (
    bundle_runtime_kernel,
    preview_kernel_manifest,
    validate_kernel_bundle,
)
from agintor.runtime.sdk.harness_executor import execute_harness_solve
from agintor.runtime.sdk.harness_entrypoint import main as harness_entry_main
from agintor.runtime.sdk.harness_manifest import HARNESS_KERNEL_CAPABILITY_FLAGS
from agintor.runtime.sdk.harness_release_loader import load_active_harness_release
from tests.mvp.test_harness_sdk_execution import (
    PassingCommandBackend,
    ScriptedRepairProvider,
    _epoch,
    _release_request,
    _source_repository,
    _task,
)


_HARNESS_FILES = {
    "__init__.py",
    "authority/__init__.py",
    "authority/public_tasks.py",
    "contracts/__init__.py",
    "contracts/epochs.py",
    "contracts/harness.py",
    "contracts/outcomes.py",
    "contracts/run_evidence.py",
    "core/__init__.py",
    "core/exceptions.py",
    "core/identity.py",
    "core/redaction.py",
    "core/versioning.py",
    "isolation/__init__.py",
    "isolation/commands.py",
    "isolation/replay.py",
    "isolation/workspaces.py",
    "repositories/__init__.py",
    "repositories/workspaces.py",
    "runtime/__init__.py",
    "runtime/api/__init__.py",
    "runtime/api/composite_compiler.py",
    "runtime/evidence.py",
    "runtime/harness_profile.py",
    "runtime/kernel/__init__.py",
    "runtime/kernel/composite_artifacts.py",
    "runtime/kernel/composite_budget.py",
    "runtime/kernel/composite_provider.py",
    "runtime/kernel/composite_replay_provider.py",
    "runtime/kernel/composite_runtime.py",
    "runtime/kernel/openai_responses_provider.py",
    "runtime/kernel/repair_tools.py",
    "runtime/sdk/__init__.py",
    "runtime/sdk/harness_entrypoint.py",
    "runtime/sdk/harness_executor.py",
    "runtime/sdk/harness_manifest.py",
    "runtime/sdk/harness_release_loader.py",
    "runtime_entry.py",
    "templates/harness/composite_compiler_metadata.json",
    "templates/harness/repo_repair_v1_two_actor_seed.json",
    "utils.py",
}
_FORBIDDEN_PARTS = {
    "evaluation",
    "factory",
    "learning",
    "oracle",
    "providers",
    "search",
    "storage",
    "tracing",
    "host",
    "langgraph",
    "tools",
}


def _expected_harness_files() -> set[str]:
    return {f"agintor_runtime/{relative}" for relative in _HARNESS_FILES}


def test_harness_profile_manifest_is_the_exact_allowlisted_closure(tmp_path: Path) -> None:
    preview = preview_kernel_manifest()
    bundled = bundle_runtime_kernel(tmp_path / "runtime", force=True)
    expected = _expected_harness_files()

    assert set(preview.files) == expected
    assert set(bundled.files) == expected
    assert preview.model_dump() == bundled.model_dump()
    assert tuple(bundled.capability_flags) == HARNESS_KERNEL_CAPABILITY_FLAGS
    assert not {
        "run_batch",
        "resume",
        "checkpoint_refs",
        "checkpoint_envelopes",
        "runtime_spec",
        "langgraph_spec",
    } & set(bundled.capability_flags)
    for path_text in bundled.files:
        parts = set(Path(path_text).parts)
        assert not parts & _FORBIDDEN_PARTS
        assert "runtime_spec" not in path_text
        assert "checkpoint" not in path_text
        assert "resume" not in path_text
        assert "session" not in path_text
        assert "predictor" not in path_text

    package = tmp_path / "runtime/runtime_sdk/agintor_runtime"
    assert (package / "contracts/__init__.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
    )
    assert (package / "runtime/__init__.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
    )
    assert (package / "runtime/api/__init__.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
    )
    assert (package / "runtime/kernel/__init__.py").read_text(encoding="utf-8") == (
        "from __future__ import annotations\n"
    )


def test_harness_bundle_detects_digest_tamper_and_unmanifested_files(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    bundle_runtime_kernel(runtime, force=True)
    package = runtime / "runtime_sdk/agintor_runtime"
    provider_path = package / "runtime/kernel/composite_provider.py"
    provider_path.write_text(
        provider_path.read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_kernel_bundle(runtime)

    bundle_runtime_kernel(runtime, force=True)
    unexpected = package / "runtime/kernel/checkpoint_resume.py"
    unexpected.write_text("UNEXPECTED = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        validate_kernel_bundle(runtime)


def _publish_factory(project_root: Path):
    epoch = _epoch()
    release, _pointer = publish_harness_release(
        project_root=project_root,
        request=_release_request(epoch),
    )
    return epoch, release


def test_source_hidden_harness_entry_inspects_only_the_active_harness_release(
    tmp_path: Path,
) -> None:
    project = tmp_path / "factory"
    _epoch_value, release = _publish_factory(project)
    bundle_root = (
        project
        / "releases"
        / release.manifest.release_digest
        / "runtime/runtime_sdk"
    )
    output = tmp_path / "inspect.json"
    script = f"""
import json
import sys
sys.path.insert(0, {str(bundle_root)!r})
from agintor_runtime.runtime.sdk.harness_entrypoint import main
raise SystemExit(main(["inspect", "--project-root", {str(project)!r}, "--output-json", {str(output)!r}]))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_kind"] == "harness"
    assert payload["capability_epoch"] == "repo-repair-v1"
    assert payload["release_digest"] == release.manifest.release_digest
    assert payload["provider_adapters"] == ["openai", "replay"]
    assert payload["command_backend_adapters"] == ["command_replay", "frozen_docker"]
    assert payload["capability_flags"] == list(HARNESS_KERNEL_CAPABILITY_FLAGS)


def test_uninstalled_live_provider_fails_before_workspace_or_backend_dispatch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "factory"
    epoch, _release = _publish_factory(project)
    source = _source_repository(tmp_path / "live-unavailable-task")
    task = _task(epoch, source)
    workspace = tmp_path / "must-not-exist"
    request = tmp_path / "live-unavailable.json"
    output = tmp_path / "live-unavailable-output.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "repo-repair-harness-solve-request-v1",
                "task": task.model_dump(mode="json"),
                "execution": {"mode": "live"},
                "run_artifact_workspace": str(workspace),
            }
        ),
        encoding="utf-8",
    )

    status = harness_entry_main(
        [
            "solve",
            "--project-root",
            str(project),
            "--request-json",
            str(request),
            "--output-json",
            str(output),
        ]
    )

    assert status == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["code"] == "provider_adapter_unavailable"
    assert "unavailable or not installed" in payload["message"]
    assert not workspace.exists()


class _RecordingProvider:
    def __init__(self, recorder: CompositeReplayRecorder, scripted: ScriptedRepairProvider) -> None:
        self.deployment_identity = scripted.deployment_identity
        self.execution_provenance = scripted.execution_provenance
        self.recorder = recorder
        self.scripted = scripted

    def invoke(self, request, *, control, credential_reference) -> ProviderInvocation:
        invocation = self.scripted.invoke(
            request,
            control=control,
            credential_reference=credential_reference,
        )
        self.recorder.record_invocation(request=request, invocation=invocation)
        return invocation


class _RecordingCommandBackend:
    def __init__(
        self,
        recorder: IsolatedCommandReplayRecorder,
        backend: PassingCommandBackend,
    ) -> None:
        self.recorder = recorder
        self.backend = backend
        self.policy = backend.policy

    def run(self, request):
        captured = self.recorder.capture_request(request)
        result = self.backend.run(request)
        self.recorder.record(
            request=captured,
            result=result,
            workspace_after=request.workspace,
        )
        return result


def test_source_hidden_entry_executes_an_explicit_exact_replay_solve(tmp_path: Path) -> None:
    project = tmp_path / "factory"
    epoch, release = _publish_factory(project)
    source = _source_repository(tmp_path / "task")
    task = _task(epoch, source)
    loaded = load_active_harness_release(project)
    plan = compile_composite_run_plan(task, loaded.protocol, loaded.dependencies)
    binding = CompositeReplayBinding.from_runtime_inputs(
        release_digest=release.manifest.release_digest,
        task=task,
        deployment=release.manifest.deployment,
        plan=plan,
    )
    recorder = CompositeReplayRecorder(binding)
    command_recorder = IsolatedCommandReplayRecorder(
        IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            command_policy_digest=(
                release.manifest.deployment.command_container_policy_digest
            ),
        )
    )
    recording_provider = _RecordingProvider(
        recorder,
        ScriptedRepairProvider(epoch.deployment),
    )
    run_id = "run.source.hidden.replay"
    workspace_id = "workspace.source.hidden.replay"

    recorded = execute_harness_solve(
        project,
        task,
        provider=recording_provider,
        command_backend=_RecordingCommandBackend(
            command_recorder,
            PassingCommandBackend(),
        ),
        run_artifact_workspace=tmp_path / "recording-run",
        run_id=run_id,
        workspace_id=workspace_id,
    )
    assert recorded.status == "completed"
    replay_manifest = recorder.finalize()
    replay_path = tmp_path / "provider-replay.json"
    write_composite_replay_manifest(replay_path, replay_manifest)
    command_replay_path = tmp_path / "command-replay.json"
    write_isolated_command_replay_manifest(
        command_replay_path,
        command_recorder.finalize(),
    )

    request_path = tmp_path / "solve-request.json"
    output_path = tmp_path / "solve-output.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "repo-repair-harness-solve-request-v1",
                "task": task.model_dump(mode="json"),
                "execution": {
                    "mode": "replay",
                    "provider_manifest_path": str(replay_path),
                    "command_manifest_path": str(command_replay_path),
                },
                "run_artifact_workspace": str(tmp_path / "replay-run"),
                "run_id": run_id,
                "workspace_id": workspace_id,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    bundle_root = (
        project
        / "releases"
        / release.manifest.release_digest
        / "runtime/runtime_sdk"
    )
    script = f"""
import sys
sys.path.insert(0, {str(bundle_root)!r})
from agintor_runtime.runtime.sdk.harness_entrypoint import main
raise SystemExit(main(["solve", "--project-root", {str(project)!r}, "--request-json", {str(request_path)!r}, "--output-json", {str(output_path)!r}]))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr + output_path.read_text(
        encoding="utf-8"
    )
    replayed = json.loads(output_path.read_text(encoding="utf-8"))
    assert replayed["status"] == "completed"
    assert replayed["run_id"] == run_id
    assert replayed["workspace_id"] == workspace_id
    assert replayed["submitted_patch"] == recorded.submitted_patch.model_dump(mode="json")
    assert replayed["budget"]["model_calls"] == len(replay_manifest.rows)
    assert replayed["budget"]["unknown_usage_events"] == 0
    assert replayed["budget"]["unknown_cost_events"] == 0


def test_built_wheel_can_materialize_and_import_the_same_harness_closure(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheel_dir),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(wheel_dir.glob("agintor-*.whl"))
    installed = tmp_path / "installed"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert install.returncode == 0, install.stderr
    runtime = tmp_path / "wheel-runtime"
    script = f"""
import json
import sys
sys.path.insert(0, {str(installed)!r})
from agintor.runtime.sdk.bundle import bundle_runtime_kernel, validate_kernel_bundle
manifest = bundle_runtime_kernel({str(runtime)!r}, force=True)
validate_kernel_bundle({str(runtime)!r})
sys.path.insert(0, {str(runtime / 'runtime_sdk')!r})
from agintor_runtime.runtime.sdk.harness_entrypoint import HarnessSolveFileRequest
print(json.dumps({{"files": sorted(manifest.files), "request": HarnessSolveFileRequest.__name__}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload["files"]) == _expected_harness_files()
    assert payload["request"] == "HarnessSolveFileRequest"
