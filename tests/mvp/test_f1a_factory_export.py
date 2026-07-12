from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import (
    EvaluatorAuthority,
    PublicReproductionStep,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    ResearchEpochManifest,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TaskEnvelope,
    TrustedToolAuthority,
    WorkspaceSnapshotRef,
)
from agintor.contracts.harness import (
    DependencyRef,
    HarnessProtocol,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.core.identity import canonical_identity_digest
from agintor.factory.harness_release import (
    advance_active_release,
    load_active_release_pointer,
    materialize_harness_release,
    publish_harness_release,
    validate_harness_generation,
)
from agintor.factory.harness_release_contracts import (
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HarnessReleaseRequest,
    PilotNotRunSummary,
    PublicSearchLineageRecord,
    PublicSelectionDecision,
)
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-f1a")


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=6,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cached_tokens=4_000,
        max_tool_calls=30,
        max_tool_output_bytes=100_000,
        max_artifact_bytes=40_000,
        max_patch_bytes=20_000,
        max_retries=1,
        max_wall_time_ms=120_000,
        provider_deadline_ms=60_000,
        max_known_cost_usd=2.0,
        max_estimated_cost_usd=3.0,
    )


def _dependencies() -> RuntimeDependencyManifest:
    return RuntimeDependencyManifest(
        compiler=DependencyRef(
            dependency_id="agintor.composite_compiler",
            interface_version="1",
            implementation_digest=_digest("compiler"),
        ),
        harness_contract=DependencyRef(
            dependency_id="agintor.harness_protocol",
            interface_version="repo-repair-harness-v1",
            implementation_digest=_digest("harness-contract"),
        ),
        kernel=DependencyRef(
            dependency_id="agintor.runtime_kernel",
            interface_version="1",
            implementation_digest=_digest("kernel"),
        ),
        trusted_tools=tuple(
            TrustedToolDependency(
                tool_id=tool_id,
                interface_version="1",
                implementation_digest=_digest(f"tool:{tool_id}"),
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in sorted(REPO_REPAIR_TRUSTED_TOOL_IDS)
        ),
    )


def _deployment_profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="openai.fixed.f1a",
        provider="openai",
        model="gpt-4.1-mini",
        endpoint=HarnessProviderEndpoint(
            base_url_env="OPENAI_BASE_URL",
            api_key_env="OPENAI_API_KEY",
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=4096,
        ),
        price_schedule=HarnessUsdPriceSchedule(
            input_usd_per_million_tokens=0.40,
            output_usd_per_million_tokens=1.60,
            cached_input_usd_per_million_tokens=0.10,
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "a" * 64,
            timeout_s=30.0,
            memory_bytes=512 * 1024 * 1024,
            cpu_count=1.0,
            pids_limit=128,
            output_bytes=1_000_000,
            tmpfs_bytes=64 * 1024 * 1024,
            nofile_limit=256,
        ),
    )


def _epoch() -> ResearchEpochManifest:
    deployment = _deployment_profile().to_deployment_identity()
    return ResearchEpochManifest(
        epoch_id="epoch.f1a",
        task_manifest_digest=_digest("task-manifest"),
        development_split_digest=_digest("development"),
        sealed_confirmation_split_digest=_digest("sealed-confirmation"),
        deployment=deployment,
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=2,
            offspring_per_step=2,
            sampling_replicates=2,
            task_panel_digest=_digest("panel"),
        ),
        trusted_tools=tuple(
            TrustedToolAuthority(
                tool_id=tool_id,
                implementation_digest=_digest(f"tool:{tool_id}"),
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
        ),
        stop_rule=StopRule(
            max_candidate_evaluations=4,
            max_consecutive_non_improving_steps=2,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="evaluator.f1a",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _task(epoch: ResearchEpochManifest) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="task.f1a.representative",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue="Repair the public regression without private target hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.f1a.public",
            uri="public-snapshot-ref",
            digest=_digest("snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=20_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _variant_protocol(label: str | None = None) -> HarnessProtocol:
    payload = load_canonical_harness_seed().protocol.model_dump(mode="python")
    if label:
        payload["actors"][0]["instruction"] = (
            f"Inspect the public repository with evidence-first strategy {label}."
        )
    return HarnessProtocol.model_validate(payload)


def _request(*, variant: str | None = None, limitation: str | None = None) -> HarnessReleaseRequest:
    epoch = _epoch()
    protocol = _variant_protocol(variant)
    dependencies = _dependencies()
    plan = compile_composite_run_plan(_task(epoch), protocol, dependencies)
    parent_digest = _variant_protocol(None).source_digest()
    selected_digest = protocol.source_digest()
    gate0 = Gate0PreregistrationPublic(
        preregistration_id="gate0.f1a",
        panel_digest=_digest("gate0-panel"),
        deterministic_suite_digest=_digest("gate0-deterministic"),
        planned_provider_calls=64,
        frozen_thresholds={
            "intact_minimum": 0.70,
            "intact_minus_null_minimum": 0.30,
            "lower_bound_minimum": 0.15,
        },
    )
    return HarnessReleaseRequest(
        epoch=epoch,
        selected_protocol=protocol,
        representative_plan=plan,
        dependency_manifest=dependencies,
        deployment_profile=_deployment_profile(),
        deployment=epoch.deployment,
        search_lineage=(
            PublicSearchLineageRecord(
                sequence_no=0,
                transaction_id="txn.f1a.selected",
                operator="instruction_rewrite",
                parent_protocol_digest=parent_digest,
                child_protocol_digest=selected_digest,
                transaction_digest=_digest(f"transaction:{variant or 'seed'}"),
                mechanism_hypothesis_digest=_digest("hypothesis"),
                status="accepted",
            ),
        ),
        selection_decisions=(
            PublicSelectionDecision(
                sequence_no=0,
                decision_id="decision.f1a.selected",
                incumbent_protocol_digest=parent_digest,
                candidate_protocol_digest=selected_digest,
                selected_protocol_digest=selected_digest,
                decision="retain_candidate" if variant else "retain_incumbent",
                reason_codes=("paired_public_outcome",),
                evidence_digests=(_digest("public-paired-evidence"),),
            ),
        ),
        gate0_preregistration=gate0,
        gate0_report=Gate0NotRunReport(
            preregistration_digest=gate0.preregistration_digest,
        ),
        pilot_summary=PilotNotRunSummary(
            pilot_id="pilot.f1a",
            planned_task_manifest_digest=_digest("pilot-task"),
        ),
        limitations=(
            limitation
            or "Real-provider Gate 0 and the non-confirmatory pilot have not been run.",
        ),
    )


def test_materialize_then_advance_builds_complete_immutable_harness_release(tmp_path: Path) -> None:
    project = tmp_path / "factory-project"
    for controlled in (".factory_chat", ".runtime_sessions", "controlled_development_and_evaluator_evidence"):
        (project / controlled).mkdir(parents=True)
        (project / controlled / "sentinel.txt").write_text("outside-release\n", encoding="utf-8")
    request = _request(variant="a")

    materialized = materialize_harness_release(project_root=project, request=request)

    assert load_active_release_pointer(project) is None
    generation = Path(materialized.generation_path)
    assert generation == project / "releases" / materialized.manifest.release_digest
    assert materialized.manifest.runtime_kind == "harness"
    assert materialized.manifest.gate0_status == "not_run"
    assert materialized.manifest.pilot_status == "not_run"
    assert validate_harness_generation(generation) == materialized.manifest
    required = {
        "public_release_evidence/release_manifest.json",
        "public_release_evidence/capability_epoch_public.json",
        "public_release_evidence/protocol/source.json",
        "public_release_evidence/protocol/compiled_plan.json",
        "public_release_evidence/protocol/consumed_field_liveness_manifest.json",
        "public_release_evidence/runtime/dependency_manifest.json",
        "public_release_evidence/search/transaction_lineage_public.jsonl",
        "public_release_evidence/search/selection_decisions_public.jsonl",
        "public_release_evidence/gate0_preregistration.json",
        "public_release_evidence/gate0_report.json",
        "public_release_evidence/pilot_summary.json",
        "public_release_evidence/limitations.md",
        "runtime/harness_protocol.json",
        "runtime/representative_composite_plan.json",
        "runtime/runtime_dependency_manifest.json",
        "runtime/evidence_index.json",
        "runtime/runtime_sdk/kernel_manifest.json",
    }
    actual = {
        path.relative_to(generation).as_posix()
        for path in generation.rglob("*")
        if path.is_file()
    }
    assert required <= actual
    projection = json.loads(
        (generation / "public_release_evidence/capability_epoch_public.json").read_text(encoding="utf-8")
    )
    serialized_projection = json.dumps(projection, sort_keys=True)
    assert "evaluator" not in serialized_projection.casefold()
    assert "sealed_confirmation" not in serialized_projection.casefold()
    for controlled in (".factory_chat", ".runtime_sessions", "controlled_development_and_evaluator_evidence"):
        assert not (generation / controlled).exists()
        assert (project / controlled / "sentinel.txt").is_file()

    pointer = advance_active_release(
        project_root=project,
        materialized=materialized,
    )

    assert pointer.release_digest == materialized.manifest.release_digest
    assert load_active_release_pointer(project) == pointer


def test_identical_release_content_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "factory-project"
    request = _request(variant="idempotent")

    first, first_pointer = publish_harness_release(project_root=project, request=request)
    before = {
        path.relative_to(Path(first.generation_path)).as_posix(): path.read_bytes()
        for path in Path(first.generation_path).rglob("*")
        if path.is_file()
    }
    second, second_pointer = publish_harness_release(project_root=project, request=request)

    assert second.manifest == first.manifest
    assert second_pointer == first_pointer
    assert len([path for path in (project / "releases").iterdir() if path.is_dir()]) == 1
    after = {
        path.relative_to(Path(second.generation_path)).as_posix(): path.read_bytes()
        for path in Path(second.generation_path).rglob("*")
        if path.is_file()
    }
    assert after == before


def test_failure_before_pointer_advance_preserves_prior_active_release(tmp_path: Path) -> None:
    project = tmp_path / "factory-project"
    first_request = _request(variant="first")
    first, first_pointer = publish_harness_release(project_root=project, request=first_request)

    def fail(_materialized) -> None:
        raise RuntimeError("injected-before-pointer")

    with pytest.raises(RuntimeError, match="injected-before-pointer"):
        publish_harness_release(
            project_root=project,
            request=_request(variant="second"),
            before_pointer_advance=fail,
        )

    assert load_active_release_pointer(project) == first_pointer
    generations = [path for path in (project / "releases").iterdir() if path.is_dir()]
    assert len(generations) == 2
    assert Path(first.generation_path) in generations


@pytest.mark.parametrize(
    "limitation",
    [
        "Credential sk-abcdefghijklmnopqrstuvwxyz012345 leaked.",
        "Source checkout was C:/Users/example/private/repository.",
        "Source checkout was /home/example/private/repository.",
    ],
)
def test_public_release_rejects_credentials_and_absolute_source_paths(
    tmp_path: Path,
    limitation: str,
) -> None:
    project = tmp_path / "factory-project"

    with pytest.raises(ValueError):
        materialize_harness_release(
            project_root=project,
            request=_request(variant="unsafe", limitation=limitation),
        )

    assert not (project / "releases").exists() or not list((project / "releases").iterdir())
    assert load_active_release_pointer(project) is None


def test_strict_release_contract_rejects_old_runtime_kinds_and_crossed_identities() -> None:
    request = _request(variant="strict")
    payload = request.model_dump(mode="python")
    payload["runtime_kind"] = "langgraph"
    with pytest.raises(ValidationError):
        HarnessReleaseRequest.model_validate(payload)

    sealed_marker_input = request.model_dump(mode="python")
    sealed_marker_input["forbidden_public_markers"] = ("sealed-value",)
    with pytest.raises(ValidationError):
        HarnessReleaseRequest.model_validate(sealed_marker_input)

    crossed = request.model_dump(mode="python")
    crossed["deployment"]["provider_config_digest"] = _digest("crossed-provider")
    with pytest.raises(ValidationError, match="deployment differs"):
        HarnessReleaseRequest.model_validate(crossed)


def test_tampered_existing_generation_is_never_reused_or_advanced(tmp_path: Path) -> None:
    project = tmp_path / "factory-project"
    request = _request(variant="tamper-check")
    materialized = materialize_harness_release(project_root=project, request=request)
    generation = Path(materialized.generation_path)
    protocol_path = generation / "runtime/harness_protocol.json"
    protocol_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="release file validation failed"):
        advance_active_release(
            project_root=project,
            materialized=materialized,
        )

    with pytest.raises(ValueError, match="release file validation failed"):
        materialize_harness_release(project_root=project, request=request)
    assert load_active_release_pointer(project) is None
