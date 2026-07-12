from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agintor.factory.harness_replay import (
    HarnessFactoryReplayRecorder,
    write_harness_factory_replay_manifest,
)
from agintor.factory.harness_service import build_harness_factory_release
from tests.mvp.test_harness_factory_replay import _multitask_evaluator
from tests.mvp.test_harness_factory_service import _build_input, _gain_proposals


def _cli(
    *arguments: str,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("AGINTOR_PROCESS_ROLE", None)
    return subprocess.run(
        [sys.executable, "-m", "agintor.cli_v1", *arguments],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        check=False,
    )


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_public_help_is_harness_only_and_module_import_does_not_load_authority_code() -> None:
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import agintor.cli_v1; "
                "assert 'agintor.evaluation.contracts' not in sys.modules; "
                "assert 'agintor.evaluation.pilot' not in sys.modules; "
                "assert 'agintor.oracle.package_io' not in sys.modules; "
                "assert 'agintor.factory.harness_service' not in sys.modules"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr

    factory_environment = dict(os.environ)
    factory_environment["AGINTOR_PROCESS_ROLE"] = "factory"
    factory_imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from agintor.factory.harness_service import HarnessFactoryBuildInput; "
                "assert 'agintor.evaluation.contracts' not in sys.modules; "
                "assert 'agintor.evaluation.runners.repo_patch_runner' not in sys.modules"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=factory_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert factory_imported.returncode == 0, factory_imported.stderr

    completed = _cli("--help")
    assert completed.returncode == 0, completed.stderr
    forwarded = subprocess.run(
        [sys.executable, "-m", "agintor.cli", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert forwarded.returncode == 0, forwarded.stderr
    for command in (
        "build-runtime",
        "eval",
        "gate0-dry-run",
        "inspect",
        "pilot-dry-run",
        "readiness-build",
        "readiness-replay",
        "search-dry-run",
        "solve",
    ):
        assert command in completed.stdout
        assert command in forwarded.stdout
    for removed in (
        "init-runtime",
        "compile-oracle",
        "evolve",
        "runtime-kind",
        "tradingagents",
        "langgraph",
    ):
        assert removed not in completed.stdout.casefold()
        assert removed not in forwarded.stdout.casefold()


def test_dry_run_uses_zero_callbacks_and_never_publishes_a_release(
    tmp_path: Path,
) -> None:
    project = (tmp_path / "dry-run-project").resolve()
    build_input, _task, _dependencies = _build_input(project, mode="dry_run")
    request_path = _write_json(
        tmp_path / "dry-run-request.json",
        build_input.model_dump(mode="json", exclude_none=True),
    )

    completed = _cli(
        "build-runtime",
        str(project),
        "--request-json",
        str(request_path),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "repo-repair-harness-cli-build-result-v1"
    assert payload["status"] == "succeeded"
    assert payload["runtime_kind"] == "harness"
    assert payload["execution_mode"] == "dry_run"
    assert payload["result"]["live_status"] == "not_run"
    assert payload["result"]["dry_run_manifest"]["callback_counts"] == {
        "evaluator": 0,
        "proposal": 0,
    }
    assert payload["result"]["dry_run_manifest"]["release_published"] is False
    assert payload["replay_provenance"] is None
    assert not (project / "active_release.json").exists()


def test_gate0_dry_run_persists_the_exact_unsent_provider_schedule(
    tmp_path: Path,
) -> None:
    from tests.mvp.test_harness_sdk_execution import _deployment_profile

    project = (tmp_path / "gate0-project").resolve()
    request_path = _write_json(
        tmp_path / "gate0-request.json",
        {
            "schema_version": "repo-repair-harness-cli-gate0-dry-run-request-v1",
            "deployment_profile": _deployment_profile().model_dump(mode="json"),
            "provider_evidence_destination": (
                "controlled_development_and_evaluator_evidence/"
                "gate0/provider_results.jsonl"
            ),
            "manifest_destination": (
                "controlled_development_and_evaluator_evidence/"
                "gate0/preregistration.json"
            ),
        },
    )

    first = _cli(
        "gate0-dry-run",
        str(project),
        "--request-json",
        str(request_path),
    )
    second = _cli(
        "gate0-dry-run",
        str(project),
        "--request-json",
        str(request_path),
    )

    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 0, second.stderr or second.stdout
    payload = json.loads(first.stdout)
    assert json.loads(second.stdout) == payload
    assert payload["schema_version"] == (
        "repo-repair-harness-cli-gate0-dry-run-result-v1"
    )
    assert payload["status"] == "succeeded"
    assert payload["live_status"] == "not_run"
    assert payload["real_inference_requests_sent"] == 0
    assert payload["planned_provider_calls"] == 1_280
    assert payload["provider_calls_sent"] == 0
    assert payload["conformance_passed"] is True
    evidence_path = Path(payload["evidence_path"])
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    manifest = persisted["manifest"]
    assert persisted["live_status"] == "not_run"
    assert manifest["manifest_digest"] == payload["manifest_digest"]
    assert manifest["live_status"] == "not_run"
    assert manifest["total_provider_calls"] == 1_280
    assert all(
        call["request_sent"] is False
        for arm in manifest["arms"]
        for call in arm["calls"]
    )
    assert not (
        project
        / "controlled_development_and_evaluator_evidence"
        / "gate0"
        / "provider_results.jsonl"
    ).exists()
    assert not (project / "active_release.json").exists()


@pytest.fixture(scope="module")
def exact_factory_replay(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("cli-v1-factory-replay")
    source_project = root / "source"
    build_input, _task, dependencies = _build_input(
        source_project,
        task_ids=("task.search.1", "task.search.2"),
    )
    proposal_calls = []
    evaluator_calls = []

    def proposals(request):
        proposal_calls.append(request)
        return _gain_proposals(index=0)(request)

    recorder = HarnessFactoryReplayRecorder(
        build_input=build_input,
        proposal_callback=proposals,
        evaluator_callback=_multitask_evaluator(
            build_input,
            dependencies,
            evaluator_calls,
        ),
    )
    recorded = build_harness_factory_release(
        build_input,
        proposal_callback=recorder.proposal_callback,
        evaluator_callback=recorder.evaluator_callback,
    )
    manifest = recorder.manifest(manifest_id="cli-v1.factory-replay")
    manifest_path = write_harness_factory_replay_manifest(
        root / "factory-replay.json",
        manifest,
    )
    return root, recorded, manifest, manifest_path


def test_offline_factory_replay_and_source_hidden_inspect_are_exact(
    exact_factory_replay,
) -> None:
    root, recorded, manifest, manifest_path = exact_factory_replay
    project = (root / "target").resolve()
    build_input, _task, _dependencies = _build_input(
        project,
        task_ids=("task.search.1", "task.search.2"),
    )
    request_path = _write_json(
        root / "target-request.json",
        build_input.model_dump(mode="json", exclude_none=True),
    )

    completed = _cli(
        "build-runtime",
        str(project),
        "--request-json",
        str(request_path),
        "--replay-manifest",
        str(manifest_path),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["status"] == "succeeded"
    assert payload["execution_mode"] == "offline_scripted"
    assert payload["result"]["release_pointer"]["release_digest"] == (
        recorded.release_pointer.release_digest
    )
    provenance = payload["replay_provenance"]
    assert provenance["replay_manifest_digest"] == manifest.manifest_digest
    assert provenance["execution_mode"] == "deterministic_replay"
    assert provenance["live_inference_status"] == "not_run"
    assert provenance["real_inference_requests_sent"] == 0
    assert provenance["provider_invocation_receipt_digests"] == []

    inspected = _cli("inspect", str(project), timeout=60.0)
    assert inspected.returncode == 0, inspected.stderr or inspected.stdout
    inspection = json.loads(inspected.stdout)
    assert inspection["status"] == "succeeded"
    assert inspection["inspection"]["runtime_kind"] == "harness"
    assert inspection["inspection"]["release_digest"] == (
        payload["result"]["release_pointer"]["release_digest"]
    )
    assert inspection["inspection"]["provider_adapters"] == ["openai", "replay"]
    assert "frozen_docker" in inspection["inspection"]["command_backend_adapters"]


def test_search_dry_run_persists_zero_callback_plan_without_changing_release(
    tmp_path: Path,
) -> None:
    project = (tmp_path / "search-dry-run-project").resolve()
    build_input, _task, _dependencies = _build_input(
        project,
        mode="dry_run",
        task_ids=("task.search.1", "task.search.2"),
    )
    request_path = _write_json(
        tmp_path / "search-dry-run-request.json",
        build_input.model_dump(mode="json", exclude_none=True),
    )
    pointer_path = project / "active_release.json"
    assert not pointer_path.exists()

    completed = _cli(
        "search-dry-run",
        str(project),
        "--request-json",
        str(request_path),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == (
        "repo-repair-harness-cli-search-dry-run-result-v1"
    )
    assert payload["status"] == "succeeded"
    assert payload["live_status"] == "not_run"
    assert payload["real_inference_requests_sent"] == 0
    assert payload["proposal_callbacks_sent"] == 0
    assert payload["evaluator_callbacks_sent"] == 0
    assert payload["release_published"] is False
    evidence_path = Path(payload["evidence_path"])
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "harness-factory-dry-run-manifest-v1"
    assert persisted["build_digest"] == payload["manifest_digest"]
    assert persisted["live_status"] == "not_run"
    assert persisted["callback_counts"] == {"evaluator": 0, "proposal": 0}
    assert all(
        opportunity["status"] == "not_run"
        and opportunity["live_status"] == "not_run"
        for group in (
            "proposal_opportunities",
            "evaluator_opportunities",
            "provider_opportunities",
        )
        for opportunity in persisted[group]
    )
    assert not pointer_path.exists()


def test_pilot_dry_run_binds_active_release_and_persists_only_unsent_calls(
    tmp_path: Path,
) -> None:
    from agintor.contracts.harness import (
        HarnessProtocol,
        RuntimeDependencyManifest,
    )
    from agintor.core.identity import evidence_digest
    from agintor.evaluation.pilot import (
        PilotDryRunManifest,
        audit_public_development_tasks,
        reserve_audited_pilot_task,
    )
    from agintor.factory.harness_release import publish_harness_release
    from agintor.runtime.api.composite_compiler import compile_composite_run_plan
    from agintor.storage.harness_session_store import HarnessSessionStore
    from tests.mvp import test_harness_sdk_execution as sdk_fixtures

    epoch = sdk_fixtures._epoch()
    project = (tmp_path / "pilot-dry-run-project").resolve()
    release, pointer = publish_harness_release(
        project_root=project,
        request=sdk_fixtures._release_request(epoch),
    )
    task = sdk_fixtures._representative_task(epoch)
    generation = project / pointer.release_path
    protocol = HarnessProtocol.model_validate(
        json.loads(
            (generation / "runtime/harness_protocol.json").read_text(
                encoding="utf-8"
            )
        )
    )
    dependencies = RuntimeDependencyManifest.model_validate(
        json.loads(
            (generation / "runtime/runtime_dependency_manifest.json").read_text(
                encoding="utf-8"
            )
        )
    )
    plan = compile_composite_run_plan(task, protocol, dependencies)
    audited = audit_public_development_tasks(
        audit_id="audit.cli-pilot-dry-run",
        epoch=epoch,
        tasks=(task,),
        inspected_at_ms=100,
    )
    audit = reserve_audited_pilot_task(
        audited,
        pilot_id="pilot.cli-dry-run",
        task_manifest_digest=task.task_manifest_digest,
        reserved_at_ms=101,
    )
    session = HarnessSessionStore(project).create_session(
        active_release_digest=release.manifest.release_digest,
        session_id="hsess.cli-pilot-dry-run",
    )
    actor_call = plan.actor_calls[0]
    run_root = (
        "controlled_development_and_evaluator_evidence/"
        "runs/cli-pilot-pair"
    )
    request_path = _write_json(
        tmp_path / "pilot-dry-run-request.json",
        {
            "schema_version": "repo-repair-harness-cli-pilot-dry-run-request-v1",
            "pilot_id": "pilot.cli-dry-run",
            "epoch": epoch.model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
            "audit": audit.model_dump(mode="json"),
            "session_id": session.session_id,
            "environment_id": "environment.cli-pilot-dry-run",
            "environment_digest": evidence_digest(
                {"kind": "cli-pilot-dry-run-environment"}
            ),
            "tool_calls": [
                {
                    "sequence": 0,
                    "call_id": "tool.cli-pilot-inspect",
                    "actor_call_id": actor_call.call_id,
                    "tool_id": actor_call.tool_ids[0],
                    "action_digest": evidence_digest(
                        {"kind": "cli-pilot-tool-action"}
                    ),
                    "max_output_bytes": 4096,
                    "call_sent": False,
                }
            ],
            "evaluator_calls": [
                {
                    "sequence": 0,
                    "call_id": "evaluator.cli-pilot-score",
                    "evaluator_id": epoch.evaluator_authority.evaluator_id,
                    "evaluator_identity_digest": (
                        epoch.evaluator_authority.evaluator_identity_digest
                    ),
                    "evaluation_contract_digest": evidence_digest(
                        {"kind": "cli-pilot-evaluation-contract"}
                    ),
                    "action_digest": evidence_digest(
                        {"kind": "cli-pilot-evaluator-action"}
                    ),
                    "call_sent": False,
                }
            ],
            "evidence_paths": [
                {
                    "purpose": "public_summary",
                    "scope": "public",
                    "relative_path": "public_release_evidence/pilot_summary.json",
                },
                {
                    "purpose": "task_audit",
                    "scope": "controlled",
                    "relative_path": (
                        "controlled_development_and_evaluator_evidence/"
                        "evaluator/task_audit_manifest.json"
                    ),
                },
                {
                    "purpose": "pilot_compiled_plan",
                    "scope": "controlled",
                    "relative_path": (
                        "controlled_development_and_evaluator_evidence/"
                        "pilot/compiled_plan.json"
                    ),
                },
                {
                    "purpose": "run_manifest",
                    "scope": "controlled",
                    "relative_path": f"{run_root}/run_manifest.json",
                },
                {
                    "purpose": "pre_call_contexts",
                    "scope": "controlled",
                    "relative_path": f"{run_root}/pre_call_contexts",
                },
                {
                    "purpose": "artifacts",
                    "scope": "controlled",
                    "relative_path": f"{run_root}/artifacts",
                },
                {
                    "purpose": "tool_receipts",
                    "scope": "controlled",
                    "relative_path": (
                        f"{run_root}/tool_and_side_effect_receipts.jsonl"
                    ),
                },
                {
                    "purpose": "outcome_receipts",
                    "scope": "controlled",
                    "relative_path": (
                        "controlled_development_and_evaluator_evidence/"
                        "evaluator/outcome_receipts.jsonl"
                    ),
                },
                {
                    "purpose": "pilot_report",
                    "scope": "controlled",
                    "relative_path": (
                        "controlled_development_and_evaluator_evidence/"
                        "analysis/pilot_report.json"
                    ),
                },
            ],
            "created_at_ms": 123,
        },
    )
    pointer_path = project / "active_release.json"
    pointer_bytes = pointer_path.read_bytes()
    release_bytes = {
        path.relative_to(generation).as_posix(): path.read_bytes()
        for path in generation.rglob("*")
        if path.is_file()
    }

    completed = _cli(
        "pilot-dry-run",
        str(project),
        "--request-json",
        str(request_path),
        timeout=120.0,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == (
        "repo-repair-harness-cli-pilot-dry-run-result-v1"
    )
    assert payload["status"] == "succeeded"
    assert payload["live_status"] == "not_run"
    assert payload["real_inference_requests_sent"] == 0
    assert payload["provider_calls_sent"] == 0
    assert payload["evaluator_calls_sent"] == 0
    manifest = PilotDryRunManifest.model_validate_json(
        Path(payload["evidence_path"]).read_text(encoding="utf-8")
    )
    assert manifest.manifest_digest == payload["manifest_digest"]
    assert manifest.active_release_digest == release.manifest.release_digest
    assert manifest.session_id == session.session_id
    assert manifest.live_status == "not_run"
    assert manifest.inference_requests_sent == 0
    assert all(not call.request_sent for call in manifest.model_calls)
    assert all(not call.call_sent for call in manifest.tool_calls)
    assert all(not call.call_sent for call in manifest.public_verification_calls)
    assert all(not call.call_sent for call in manifest.evaluator_calls)
    assert pointer_path.read_bytes() == pointer_bytes
    assert {
        path.relative_to(generation).as_posix(): path.read_bytes()
        for path in generation.rglob("*")
        if path.is_file()
    } == release_bytes


def test_inspect_and_replay_environment_scrub_frozen_credential_names(
    exact_factory_replay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, recorded, _manifest, _manifest_path = exact_factory_replay
    source_project = root / "source"
    generation = (
        source_project
        / recorded.release_pointer.release_path
    )
    sentinel = "SENTINEL_AMBIENT_PROVIDER_CREDENTIAL_MUST_NOT_CROSS"
    monkeypatch.setenv("SCRIPTED_PROVIDER_API_KEY", sentinel)

    from agintor.cli_v1 import _child_environment

    replay_environment = _child_environment(
        generation,
        role="runtime",
        allow_credentials=False,
    )
    assert "SCRIPTED_PROVIDER_API_KEY" not in replay_environment
    live_environment = _child_environment(
        generation,
        role="runtime",
        allow_credentials=True,
    )
    assert live_environment["SCRIPTED_PROVIDER_API_KEY"] == sentinel

    inspected = _cli("inspect", str(source_project), timeout=60.0)
    assert inspected.returncode == 0, inspected.stderr or inspected.stdout
    assert sentinel not in inspected.stdout
    assert sentinel not in inspected.stderr


def test_live_key_file_is_exclusive_and_must_remain_outside_runtime_roots(
    exact_factory_replay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, recorded, _manifest, _manifest_path = exact_factory_replay
    source_project = root / "source"
    generation = source_project / recorded.release_pointer.release_path
    profile_path = generation / "runtime" / "runtime_profile.json"
    projection = json.loads(profile_path.read_text(encoding="utf-8"))
    projection["profile"]["endpoint"]["api_key_file_env"] = (
        "SCRIPTED_PROVIDER_API_KEY_FILE"
    )
    profile_path.write_text(json.dumps(projection), encoding="utf-8")

    from agintor.cli_v1 import CliV1Error, _child_environment

    ambient_secret = "sk-proj-test-ambient-must-not-cross"
    monkeypatch.setenv("SCRIPTED_PROVIDER_API_KEY", ambient_secret)
    external_key = root / "external-provider-key.txt"
    external_key.write_text("sk-proj-test-file-only", encoding="utf-8")

    environment = _child_environment(
        generation,
        role="runtime",
        api_key_file=str(external_key),
        allow_credentials=True,
        forbidden_credential_roots=(source_project,),
    )
    assert "SCRIPTED_PROVIDER_API_KEY" not in environment
    assert environment["SCRIPTED_PROVIDER_API_KEY_FILE"] == str(external_key.resolve())
    assert ambient_secret not in json.dumps(environment)

    repository_key = source_project / "provider-key.txt"
    repository_key.write_text("sk-proj-test-repository-copy", encoding="utf-8")
    with pytest.raises(CliV1Error, match="outside project"):
        _child_environment(
            generation,
            role="runtime",
            api_key_file=str(repository_key),
            allow_credentials=True,
            forbidden_credential_roots=(source_project,),
        )


def test_public_error_payloads_redact_resolved_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agintor.cli_v1 import CliV1Error, _error_payload
    from agintor.runtime.sdk.harness_entrypoint import HarnessEntryError, _error_result

    secret = "sk-proj-test-public-error-redaction"
    key_path = "C:/private/provider-key.txt"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("AGINTOR_OPENAI_KEY_FILE", key_path)

    cli_payload = _error_payload(
        "solve",
        CliV1Error("provider_failed", f"provider rejected {secret} from {key_path}"),
    )
    entry_payload = _error_result(
        HarnessEntryError(
            "provider_failed",
            f"provider rejected {secret} from {key_path}",
        )
    ).model_dump(mode="json")

    serialized = json.dumps((cli_payload, entry_payload))
    assert secret not in serialized
    assert key_path not in serialized
    assert serialized.count("[REDACTED_CREDENTIAL]") >= 4


def test_serial_factory_followup_starts_from_exact_active_protocol_transactionally(
    exact_factory_replay,
) -> None:
    from agintor.contracts.harness import HarnessProtocol
    from agintor.search.paired_harness import run_paired_harness_search

    root, _recorded, _manifest, initial_manifest_path = exact_factory_replay
    project = (root / "followup-target").resolve()
    initial_input, _task, _dependencies = _build_input(
        project,
        task_ids=("task.search.1", "task.search.2"),
    )
    initial_request = _write_json(
        root / "followup-initial-request.json",
        initial_input.model_dump(mode="json", exclude_none=True),
    )
    initial_completed = _cli(
        "build-runtime",
        str(project),
        "--request-json",
        str(initial_request),
        "--replay-manifest",
        str(initial_manifest_path),
    )
    assert initial_completed.returncode == 0, initial_completed.stderr or initial_completed.stdout
    initial = json.loads(initial_completed.stdout)
    initial_pointer = initial["result"]["release_pointer"]
    initial_message = initial["result"]["factory_message"]
    from agintor.storage.harness_session_store import HarnessSessionStore

    session_store = HarnessSessionStore(project)
    old_session = session_store.create_session(
        active_release_digest=initial_pointer["release_digest"],
        session_id="hsess.cli-old-release",
    )
    active_protocol = HarnessProtocol.model_validate(
        json.loads(
            (
                project
                / initial_pointer["release_path"]
                / "runtime/harness_protocol.json"
            ).read_text(encoding="utf-8")
        )
    )
    assert active_protocol.source_digest() == initial["result"]["selected_protocol_digest"]

    followup_input, _task, dependencies = _build_input(
        project,
        prompt="Retain a second structural descendant through exact replay.",
        founding_protocol=active_protocol,
        task_ids=("task.search.1", "task.search.2"),
        expected_parent_message_id=initial_message["message_id"],
        expected_message_index=1,
    )
    evaluator_calls = []
    recorder = HarnessFactoryReplayRecorder(
        build_input=followup_input,
        proposal_callback=_gain_proposals(index=1),
        evaluator_callback=_multitask_evaluator(
            followup_input,
            dependencies,
            evaluator_calls,
        ),
    )
    run_paired_harness_search(
        epoch=followup_input.epoch,
        tasks=followup_input.task_panel,
        dependency_manifest=followup_input.dependency_manifest,
        founding_protocol=followup_input.founding_protocol,
        config=followup_input.s1_config,
        proposal_callback=recorder.proposal_callback,
        evaluator_callback=recorder.evaluator_callback,
    )
    followup_manifest = recorder.manifest(
        manifest_id="cli-v1.factory-followup-replay"
    )
    followup_manifest_path = write_harness_factory_replay_manifest(
        root / "factory-followup-replay.json",
        followup_manifest,
    )
    followup_request = _write_json(
        root / "followup-request.json",
        followup_input.model_dump(mode="json", exclude_none=True),
    )
    followup_completed = _cli(
        "build-runtime",
        str(project),
        "--request-json",
        str(followup_request),
        "--replay-manifest",
        str(followup_manifest_path),
    )

    assert followup_completed.returncode == 0, (
        followup_completed.stderr or followup_completed.stdout
    )
    followup = json.loads(followup_completed.stdout)
    message = followup["result"]["factory_message"]
    pointer = followup["result"]["release_pointer"]
    assert message["message_index"] == 1
    assert message["parent_message_id"] == initial_message["message_id"]
    assert message["prior_active_release_digest"] == initial_pointer["release_digest"]
    assert pointer["release_digest"] != initial_pointer["release_digest"]
    assert (project / initial_pointer["release_path"]).is_dir()
    assert (project / pointer["release_path"]).is_dir()
    persisted_pointer = json.loads(
        (project / "active_release.json").read_text(encoding="utf-8")
    )
    assert persisted_pointer == pointer

    followup_task_path = _write_json(
        root / "followup-task.json",
        followup_input.task_panel[0].model_dump(mode="json"),
    )
    rejected_workspace = root / "old-session-rejected-run"
    rejected = _cli(
        "solve",
        str(project),
        "--task-envelope",
        str(followup_task_path),
        "--live",
        "--run-root",
        str(rejected_workspace),
        "--session",
        old_session.session_id,
        timeout=60.0,
    )
    assert rejected.returncode == 2
    rejected_payload = json.loads(rejected.stdout)
    assert rejected_payload["code"] == "session_release_mismatch"
    assert "start a new session" in rejected_payload["message"]
    assert not rejected_workspace.exists()
    assert session_store.recover(old_session.session_id).message_count == 0


def test_eval_is_a_separate_evaluator_role_subprocess_with_public_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agintor.evaluation.harness_entrypoint import (
        HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
    )
    from agintor.evaluation.runners.repo_patch_runner import (
        RepoPatchEvaluatorRunner,
        RepoPatchFixture,
    )
    from agintor.isolation.replay import (
        IsolatedCommandReplayBinding,
        IsolatedCommandReplayManifest,
        write_isolated_command_replay_manifest,
    )
    from tests.mvp.test_harness_evaluation_service import (
        CANARY,
        GOOD_PATCH,
        TranscriptOnlyIsolatedBackend,
        _isolated_backend,
        _profile,
        _setup,
    )

    service, _, _, epoch, task, contract, pair_key, _ = _setup(
        tmp_path,
        monkeypatch,
    )
    transcript = TranscriptOnlyIsolatedBackend()
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
    )
    RepoPatchEvaluatorRunner(_isolated_backend(transcript)).run(
        candidate_artifact=GOOD_PATCH,
        fixture=fixture,
    )
    active = json.loads(
        (service.project_root / "active_release.json").read_text(encoding="utf-8")
    )
    replay_path = tmp_path / "evaluator-commands.json"
    write_isolated_command_replay_manifest(
        replay_path,
        IsolatedCommandReplayManifest(
            binding=IsolatedCommandReplayBinding.from_runtime_inputs(
                release_digest=active["release_digest"],
                task=task,
                command_policy_digest=_profile().command_container_policy_digest,
            ),
            rows=tuple(transcript.rows),
        ),
    )
    request_path = _write_json(
        tmp_path / "evaluator-request.json",
        {
            "schema_version": HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
            "operation": "dry_run",
            "execution": {
                "mode": "replay",
                "command_manifest_path": replay_path.name,
            },
            "epoch": epoch.model_dump(mode="json"),
            "contract": contract.model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
            "submitted_unified_diff": GOOD_PATCH,
            "pair_key": pair_key.model_dump(mode="json"),
            "proof_store_root": "controlled-cli-proofs",
        },
    )
    output_path = tmp_path / "cli-evaluator-output.json"

    completed = _cli(
        "eval",
        "--project-root",
        str(service.project_root),
        "--request-json",
        str(request_path),
        "--output-json",
        str(output_path),
        timeout=120.0,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload == json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "repo-repair-harness-cli-eval-result-v1"
    assert payload["status"] == "succeeded"
    assert payload["result"]["status"] == "not_run"
    assert payload["result"]["backend_invocations"] == 0
    assert payload["result"]["real_docker_requests_sent"] == 0
    serialized = output_path.read_text(encoding="utf-8")
    assert CANARY not in serialized
    assert "evaluation_contract_digest" not in serialized
    assert not (tmp_path / "controlled-cli-proofs").exists()

    release_root = service.project_root / "releases" / active["release_digest"]
    release_bytes = {
        path.relative_to(release_root).as_posix(): path.read_bytes()
        for path in release_root.rglob("*")
        if path.is_file()
    }
    pointer_bytes = (service.project_root / "active_release.json").read_bytes()
    request_bytes = request_path.read_bytes()
    unsafe_outputs = (
        release_root / "public_release_evidence/cli-eval-output.json",
        tmp_path / "controlled-cli-proofs/output.json",
        Path(task.workspace_snapshot.uri) / "cli-eval-output.json",
        request_path,
    )
    for unsafe_output in unsafe_outputs:
        rejected = _cli(
            "eval",
            "--project-root",
            str(service.project_root),
            "--request-json",
            str(request_path),
            "--output-json",
            str(unsafe_output),
            timeout=60.0,
        )
        assert rejected.returncode == 2
        rejection = json.loads(rejected.stdout)
        assert rejection["code"] in {
            "output_path_invalid",
            "proof_store_path_invalid",
            "public_output_path_invalid",
        }
        if unsafe_output != request_path:
            assert not unsafe_output.exists()
    assert (service.project_root / "active_release.json").read_bytes() == pointer_bytes
    assert request_path.read_bytes() == request_bytes
    assert {
        path.relative_to(release_root).as_posix(): path.read_bytes()
        for path in release_root.rglob("*")
        if path.is_file()
    } == release_bytes


def test_solve_uses_exact_dual_replay_source_hidden_runtime_and_session_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agintor.contracts.outcomes import PairKey
    from agintor.factory.harness_release import publish_harness_release
    from agintor.isolation.replay import (
        IsolatedCommandReplayBinding,
        IsolatedCommandReplayRecorder,
        write_isolated_command_replay_manifest,
    )
    from agintor.runtime.api.composite_compiler import compile_composite_run_plan
    from agintor.runtime.kernel.composite_replay_provider import (
        CompositeReplayBinding,
        CompositeReplayRecorder,
        write_composite_replay_manifest,
    )
    from agintor.runtime.sdk.harness_executor import execute_harness_solve
    from agintor.storage.harness_session_store import HarnessSessionStore
    from tests.mvp import test_harness_sdk_execution as sdk_fixtures
    from tests.mvp.test_isolated_command_replay import (
        _RecordingCommandBackend,
        _RecordingProvider,
    )

    epoch = sdk_fixtures._epoch()
    project = tmp_path / "solve-factory"
    release, _pointer = publish_harness_release(
        project_root=project,
        request=sdk_fixtures._release_request(epoch),
    )
    source = sdk_fixtures._source_repository(tmp_path / "solve-source")
    task = sdk_fixtures._task(epoch, source)
    task_payload = task.model_dump(mode="json")
    task_payload.pop("task_manifest_digest")
    task_payload["workspace_snapshot"]["uri"] = source.relative_to(tmp_path).as_posix()
    task = type(task).model_validate(task_payload)
    relative_task_digest = task.task_manifest_digest
    assert not Path(task.workspace_snapshot.uri).is_absolute()
    session_store = HarnessSessionStore(project)
    session_manifest = session_store.create_session(
        active_release_digest=release.manifest.release_digest,
        session_id="hsess.cli-exact-replay",
    )
    session_context = session_store.context_for_next(
        session_manifest.session_id,
        active_release_digest=release.manifest.release_digest,
    )
    public_context = session_context.to_public_runtime_context()
    from agintor.contracts.harness import (
        HarnessPublicCarryoverRef,
        HarnessPublicSessionContext,
        HarnessPublicSessionLimits,
        public_session_context_digest,
    )

    public_context_roundtrip = HarnessPublicSessionContext.model_validate(
        public_context.model_dump(mode="json")
    )
    assert public_context_roundtrip.context_digest == public_context.context_digest
    assert public_session_context_digest(public_context) == public_context.context_digest
    assert (
        public_session_context_digest(public_context.model_dump(mode="json"))
        == public_context.context_digest
    )
    plan = compile_composite_run_plan(
        task,
        sdk_fixtures.load_canonical_harness_seed().protocol,
        sdk_fixtures._dependencies(),
    )
    nested_context = HarnessPublicSessionContext(
        session_id="hsess.cli-digest-regression",
        active_release_digest=release.manifest.release_digest,
        session_manifest_digest=session_manifest.manifest_digest,
        next_sequence=3,
        parent_message_id="hmsg.cli-digest-regression",
        limits=HarnessPublicSessionLimits(
            max_entries=3,
            max_total_bytes=8192,
            max_summary_bytes=1024,
        ),
        carryover=(
            HarnessPublicCarryoverRef(
                artifact_ref="runs/prior/harness_solve_result.json",
                artifact_digest=epoch.epoch_manifest_digest,
                summary="Public prior solve result reference.",
            ),
        ),
    )
    nested_roundtrip = HarnessPublicSessionContext.model_validate(
        nested_context.model_dump(mode="json")
    )
    nested_binding = CompositeReplayBinding.from_runtime_inputs(
        release_digest=release.manifest.release_digest,
        task=task,
        deployment=epoch.deployment,
        plan=plan,
        public_session_context=nested_roundtrip,
    )
    assert nested_roundtrip.context_digest == nested_context.context_digest
    assert nested_binding.public_session_context_digest == nested_context.context_digest
    assert (
        public_session_context_digest(nested_context.model_dump(mode="json"))
        == nested_context.context_digest
    )
    provider_recorder = CompositeReplayRecorder(
        CompositeReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            deployment=epoch.deployment,
            plan=plan,
            public_session_context=public_context,
        )
    )
    command_recorder = IsolatedCommandReplayRecorder(
        IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            command_policy_digest=epoch.deployment.command_container_policy_digest,
        )
    )
    run_id = "run.cli-v1-exact-replay"
    workspace_id = "workspace.cli-v1-exact-replay"
    pair_key = PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id="environment.cli-v1-exact-replay",
        sampling_replicate=0,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )
    captured_requests = []
    recording_provider = _RecordingProvider(
        sdk_fixtures.ScriptedRepairProvider(epoch.deployment),
        provider_recorder,
    )

    class CapturingProvider:
        execution_provenance = recording_provider.execution_provenance
        deployment_identity = recording_provider.deployment_identity

        def invoke(self, request, *, control, credential_reference):
            captured_requests.append(request)
            return recording_provider.invoke(
                request,
                control=control,
                credential_reference=credential_reference,
            )

    recorded = execute_harness_solve(
        project,
        task,
        provider=CapturingProvider(),
        command_backend=_RecordingCommandBackend(
            sdk_fixtures.PassingCommandBackend(),
            command_recorder,
        ),
        run_artifact_workspace=tmp_path / "recorded-workspace",
        run_id=run_id,
        workspace_id=workspace_id,
        pair_key=pair_key,
        public_session_context=public_context,
        snapshot_source_root=source,
    )
    assert recorded.status == "completed", {
        "public_context": public_context.model_dump(mode="json"),
        "binding": provider_recorder.binding.model_dump(mode="json"),
        "reads": [
            read.model_dump(mode="json")
            for request in captured_requests
            for read in request.context.reads
            if read.source_kind == "session"
        ],
    }
    provider_manifest = tmp_path / "solve-provider-replay.json"
    command_manifest = tmp_path / "solve-command-replay.json"
    write_composite_replay_manifest(provider_manifest, provider_recorder.finalize())
    write_isolated_command_replay_manifest(
        command_manifest,
        command_recorder.finalize(),
    )
    task_path = _write_json(
        tmp_path / "solve-task.json",
        task.model_dump(mode="json"),
    )
    reserved_task_path = tmp_path / "sealed" / "public-task.json"
    reserved_task_path.parent.mkdir()
    reserved_task_path.write_text("{ this must not be parsed", encoding="utf-8")
    rejected_reserved_task = _cli(
        "solve",
        str(project),
        "--task-envelope",
        str(reserved_task_path),
        "--live",
        "--run-root",
        str(tmp_path / "reserved-task-run-root"),
        "--new-session",
        timeout=60.0,
    )
    assert rejected_reserved_task.returncode == 2
    rejected_payload = json.loads(rejected_reserved_task.stdout)
    assert rejected_payload["code"] == "task_envelope_invalid"
    assert (
        "public task envelope failed strict public loader validation"
        in rejected_payload["message"]
    )
    assert not (tmp_path / "reserved-task-run-root").exists()

    pair_key_path = _write_json(
        tmp_path / "solve-pair-key.json",
        pair_key.model_dump(mode="json"),
    )
    replay_workspace = tmp_path / "replayed-workspace"
    sentinel = "SENTINEL_REPLAY_CREDENTIAL_MUST_NEVER_REACH_PROVIDER"
    monkeypatch.setenv("SCRIPTED_PROVIDER_API_KEY", sentinel)

    completed = _cli(
        "solve",
        str(project),
        "--task-envelope",
        str(task_path),
        "--pair-key",
        str(pair_key_path),
        "--replay-provider-manifest",
        str(provider_manifest),
        "--replay-command-manifest",
        str(command_manifest),
        "--workspace",
        str(replay_workspace),
        "--run-id",
        run_id,
        "--workspace-id",
        workspace_id,
        "--session",
        session_manifest.session_id,
        timeout=120.0,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert task.task_manifest_digest == relative_task_digest
    assert payload["schema_version"] == "repo-repair-harness-cli-solve-result-v1"
    assert payload["status"] == "succeeded"
    assert payload["session"] == {
        "session_id": session_manifest.session_id,
        "message_id": payload["session"]["message_id"],
        "sequence": 0,
        "active_release_digest": release.manifest.release_digest,
    }
    result = payload["result"]
    assert result["status"] == "completed"
    assert result["execution_mode"] == "deterministic_replay"
    assert result["live_inference_status"] == "not_run"
    assert result["real_inference_requests_sent"] == 0
    assert result["capability_promotion_authorized"] is False
    assert result["eligible_for_evaluator_submission"] is True
    assert result["controlled_run_evidence"] is not None
    assert result["controlled_run_evidence"]["pair_key_digest"]
    controlled_evidence_path = (
        replay_workspace
        / result["controlled_run_evidence"]["relative_path"]
    )
    controlled_evidence = json.loads(
        controlled_evidence_path.read_text(encoding="utf-8")
    )
    assert controlled_evidence["pair_key"] == pair_key.model_dump(mode="json")
    assert controlled_evidence["evidence_digest"] == (
        result["controlled_run_evidence"]["evidence_digest"]
    )
    assert result["evidence"]["contexts"]
    assert sentinel not in completed.stdout
    assert sentinel not in completed.stderr
    assert (source / "src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "old"\n'
    )
    assert (replay_workspace / "repository/working/src/app.py").read_text(
        encoding="utf-8"
    ) == 'def value():\n    return "new"\n'
    continued = session_store.context_for_next(
        session_manifest.session_id,
        active_release_digest=release.manifest.release_digest,
    )
    assert continued.next_sequence == 1
    assert continued.parent_message_id == payload["session"]["message_id"]
    assert len(continued.carryover) == 1
    assert continued.carryover[0].artifact_digest == result["result_digest"]

    prior_sessions = set(session_store.root.iterdir())
    unavailable = _cli(
        "solve",
        str(project),
        "--task-envelope",
        str(task_path),
        "--live",
        "--run-root",
        str(tmp_path / "live-run-root"),
        "--new-session",
        timeout=60.0,
    )
    assert unavailable.returncode == 2
    unavailable_payload = json.loads(unavailable.stdout)
    assert unavailable_payload["code"] == "provider_adapter_unavailable"
    assert sentinel not in unavailable.stdout
    new_sessions = set(session_store.root.iterdir()) - prior_sessions
    assert len(new_sessions) == 1
    empty_session = session_store.recover(next(iter(new_sessions)).name)
    assert empty_session.message_count == 0
    assert empty_session.active_release_digest == release.manifest.release_digest


def test_build_project_mismatch_is_stable_json_and_has_no_side_effect(
    tmp_path: Path,
) -> None:
    project = (tmp_path / "expected").resolve()
    crossed_project = (tmp_path / "crossed").resolve()
    build_input, _task, _dependencies = _build_input(
        crossed_project,
        mode="dry_run",
    )
    request_path = _write_json(
        tmp_path / "crossed-request.json",
        build_input.model_dump(mode="json", exclude_none=True),
    )

    completed = _cli(
        "build-runtime",
        str(project),
        "--request-json",
        str(request_path),
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload == {
        "schema_version": "repo-repair-harness-cli-error-v1",
        "status": "failed",
        "operation": "build-runtime",
        "code": "build_project_mismatch",
        "message": "build request project_root must exactly match the CLI factory project",
    }
    assert not project.exists()
    assert not (project / "active_release.json").exists()
