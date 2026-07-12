from __future__ import annotations

import json
import os
import uuid
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
from agintor.contracts.harness_actions import (
    InstructionRewritePatch,
    TransactionApplicability,
)
from agintor.contracts.outcomes import PairKey
from agintor.core.identity import canonical_identity_digest
from agintor.contracts.feasibility import (
    BaselineHeadroomAssessment,
    D0LiveBaselineProof,
    DevelopmentTaskFeasibilityManifest,
    FeasibilityControlResult,
    PairedSearchBudgetProjection,
    ProviderBaselineDryRun,
    d0_evaluation_contract_authority_digest,
)
from agintor.evaluation.gate0 import (
    build_gate0_dry_run_manifest,
    build_gate0_provider_identity,
    validate_gate0_dry_run_conformance,
    require_gate0_live_authorization,
)
from agintor.evaluation.feasibility import (
    D0LiveBaselineReport,
    DevelopmentTaskFeasibilityRunner,
    d0_live_baseline_public_proof,
    require_d0_live_authorization,
)
from agintor.evaluation.gate0_runner import GATE0_LIVE_ENABLE_ENV, run_gate0_live
from agintor.evaluation.runners.repo_patch_runner import (
    RepoPatchFixture,
    environment_digest as repo_patch_environment_digest,
    repo_snapshot_digest,
)
from agintor.factory.harness_release import load_active_release_pointer
from agintor.factory.harness_service import (
    HarnessFactoryBuildInput,
    HarnessFactoryExecutionModeError,
    HarnessFactoryServiceValidationError,
    build_harness_factory_release,
)
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
    load_composite_compiler_metadata,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
    harness_deployment_profile_digest,
)
from agintor.search.paired_harness import (
    CompiledTaskPlan,
    HarnessEvaluationRequest,
    LiveSearchAuthorization,
    PairedHarnessSearchConfig,
    ProposalBatchRequest,
    canonical_pair_keys,
    paired_task_panel_digest,
)
from agintor.runtime.kernel.composite_provider import CredentialReference
from tests.mvp.test_s1_paired_search import (
    _channel_proposal,
    _controls,
    _proposal,
    _receipt,
    _scripted_evaluator,
)
from tests.mvp.test_d0_task_feasibility import (
    _contract as _d0_contract,
    _good_patch as _d0_good_patch,
    _isolated_backend as _d0_isolated_backend,
    _wrong_patch as _d0_wrong_patch,
    _write_source as _d0_write_source,
)
from tests.mvp.test_g0_provider_runner import ScriptedExecutor as Gate0ScriptedExecutor


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-factory-service")


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=8,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cached_tokens=10_000,
        max_tool_calls=30,
        max_tool_output_bytes=100_000,
        max_artifact_bytes=100_000,
        max_patch_bytes=30_000,
        max_retries=2,
        max_wall_time_ms=120_000,
        provider_deadline_ms=30_000,
        max_known_cost_usd=5.0,
        max_estimated_cost_usd=6.0,
    )


def _dependencies() -> RuntimeDependencyManifest:
    metadata = load_composite_compiler_metadata()
    return RuntimeDependencyManifest(
        compiler=DependencyRef(
            dependency_id=metadata.compiler_id,
            interface_version=metadata.compiler_version,
            implementation_digest=_digest("compiler"),
        ),
        harness_contract=DependencyRef(
            dependency_id=metadata.harness_contract_id,
            interface_version=metadata.harness_schema_version,
            implementation_digest=_digest("harness-contract"),
        ),
        kernel=DependencyRef(
            dependency_id="agintor.composite_kernel",
            interface_version="kernel-v1",
            implementation_digest=_digest("kernel"),
        ),
        trusted_tools=tuple(
            TrustedToolDependency(
                tool_id=tool_id,
                interface_version="repo-tool-v1",
                implementation_digest=_digest(f"tool-impl:{tool_id}"),
                policy_digest=_digest(f"tool-policy:{tool_id}"),
            )
            for tool_id in sorted(REPO_REPAIR_TRUSTED_TOOL_IDS)
        ),
    )


def _pair_keys(
    provider_config_digest: str,
    task_ids: tuple[str, ...] = ("task.search.1",),
) -> tuple[PairKey, ...]:
    return canonical_pair_keys(
        tuple(
            PairKey(
                task_manifest_id=task_id,
                environment_id=f"environment.{task_id}",
                sampling_replicate=replicate,
                provider_config_digest=provider_config_digest,
            )
            for task_id in task_ids
            for replicate in range(2)
        )
    )


def _deployment_profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="scripted.harness.service",
        provider="scripted",
        model="scripted-repair-model",
        endpoint=HarnessProviderEndpoint(
            base_url_env="SCRIPTED_BASE_URL",
            api_key_env="SCRIPTED_PROVIDER_API_KEY",
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=4096,
        ),
        price_schedule=HarnessUsdPriceSchedule(
            billing_mode="free",
            input_usd_per_million_tokens=0.0,
            output_usd_per_million_tokens=0.0,
            cached_input_usd_per_million_tokens=0.0,
            provider_policy_justification="scripted factory service fixture has no provider billing",
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "c" * 64,
            timeout_s=30.0,
            memory_bytes=512 * 1024 * 1024,
            cpu_count=1.0,
            pids_limit=128,
            output_bytes=1_000_000,
            tmpfs_bytes=64 * 1024 * 1024,
            nofile_limit=256,
        ),
    )


def _provider_identity(
    deployment_profile: HarnessDeploymentProfile | None = None,
):
    return build_gate0_provider_identity(
        deployment_profile=deployment_profile or _deployment_profile(),
    )


def _variant_deployment_profile(**updates) -> HarnessDeploymentProfile:
    payload = _deployment_profile().profile_payload()
    payload.update(updates)
    return HarnessDeploymentProfile.model_validate(payload)


def _epoch(
    dependencies: RuntimeDependencyManifest,
    pair_keys: tuple[PairKey, ...],
) -> ResearchEpochManifest:
    deployment = _deployment_profile().to_deployment_identity()
    return ResearchEpochManifest(
        epoch_id="epoch.factory.service",
        task_manifest_digest=_digest("task-distribution"),
        development_split_digest=_digest("development-split"),
        sealed_confirmation_split_digest=_digest("sealed-split"),
        deployment=deployment,
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=1,
            offspring_per_step=1,
            sampling_replicates=2,
            task_panel_digest=paired_task_panel_digest(pair_keys),
        ),
        trusted_tools=tuple(
            TrustedToolAuthority(
                tool_id=tool_id,
                implementation_digest=next(
                    tool.implementation_digest
                    for tool in dependencies.trusted_tools
                    if tool.tool_id == tool_id
                ),
                policy_digest=next(
                    tool.policy_digest
                    for tool in dependencies.trusted_tools
                    if tool.tool_id == tool_id
                ),
            )
            for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
        ),
        stop_rule=StopRule(
            max_candidate_evaluations=1,
            max_consecutive_non_improving_steps=2,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="eval.factory.service",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _task(
    epoch: ResearchEpochManifest,
    *,
    task_manifest_id: str = "task.search.1",
    index: int = 1,
    source: Path,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id=task_manifest_id,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue=f"Repair public regression {index} without sealed hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id=f"snapshot.factory.service.{index}",
            uri=f"cas://snapshot-factory-service-{index}",
            digest=repo_snapshot_digest(source),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=(
                    "python",
                    "-c",
                    (
                        "from pathlib import Path\n"
                        "text = Path('src/app.py').read_text(encoding='utf-8')\n"
                        "assert 'VALUE = 2' in text, text\n"
                    ),
                ),
                timeout_ms=30_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _d0(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    pair_keys: tuple[PairKey, ...],
    protocol_digest: str,
    *,
    status: str = "pending_real_provider_baseline",
) -> DevelopmentTaskFeasibilityManifest:
    controls_passed = status == "pending_real_provider_baseline"
    projection_fits = controls_passed
    return DevelopmentTaskFeasibilityManifest(
        manifest_id="d0.factory.service",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id="evaluation.factory.service",
        evaluation_contract_digest=_digest("evaluation-contract"),
        execution_backend_id="isolated.repo.patch",
        execution_backend_digest=_digest("execution-backend"),
        controls=(
            FeasibilityControlResult(
                control_id="control.known_good",
                control_kind="known_good",
                artifact_digest=_digest("known-good"),
                expected_complete_repair=True,
                observed_complete_repair=True,
                evaluator_status="passed",
                outcome_fingerprint="known-good",
                source_snapshot_unchanged=True,
                scratch_snapshot_matched=True,
                fixture_identity_matched=True,
                protected_tamper_detected=True,
                passed=controls_passed,
            ),
            FeasibilityControlResult(
                control_id="control.empty",
                control_kind="empty",
                artifact_digest=_digest("empty"),
                expected_complete_repair=False,
                observed_complete_repair=False,
                evaluator_status="failed",
                outcome_fingerprint="empty",
                source_snapshot_unchanged=True,
                scratch_snapshot_matched=True,
                fixture_identity_matched=True,
                protected_tamper_detected=True,
                passed=controls_passed,
            ),
            FeasibilityControlResult(
                control_id="control.wrong",
                control_kind="plausible_wrong",
                artifact_digest=_digest("wrong"),
                expected_complete_repair=False,
                observed_complete_repair=False,
                evaluator_status="failed",
                outcome_fingerprint="wrong",
                source_snapshot_unchanged=True,
                scratch_snapshot_matched=True,
                fixture_identity_matched=True,
                protected_tamper_detected=True,
                passed=controls_passed,
            ),
            FeasibilityControlResult(
                control_id="control.tamper",
                control_kind="protected_tamper",
                artifact_digest=_digest("tamper"),
                expected_complete_repair=False,
                observed_complete_repair=False,
                evaluator_status="blocked",
                outcome_fingerprint="tamper",
                source_snapshot_unchanged=True,
                scratch_snapshot_matched=True,
                fixture_identity_matched=True,
                protected_tamper_detected=True,
                passed=controls_passed,
            ),
        ),
        clean_replay_reproducible=controls_passed,
        protected_path_integrity=controls_passed,
        leakage_integrity=controls_passed,
        identity_integrity=controls_passed,
        offline_controls_passed=controls_passed,
        baseline_headroom=BaselineHeadroomAssessment(
            status="not_measured",
            receipt_count=0,
            complete_repairs=0,
            failures=0,
        ),
        paired_search_projection=PairedSearchBudgetProjection(
            structural_candidate_capacity=1,
            frozen_candidate_budget=epoch.stop_rule.max_candidate_evaluations,
            projected_candidate_evaluations=1,
            sampling_replicates=epoch.search_envelope.sampling_replicates,
            projected_paired_outcome_runs=4,
            projected_max_model_calls=8,
            projected_max_known_cost_usd=1.0,
            projected_max_estimated_cost_usd=1.0,
            frozen_max_model_calls=epoch.per_run_ceilings.max_model_calls,
            frozen_max_known_cost_usd=epoch.per_run_ceilings.max_known_cost_usd,
            frozen_max_estimated_cost_usd=epoch.per_run_ceilings.max_estimated_cost_usd,
            fits_frozen_epoch_budget=projection_fits,
        ),
        provider_baseline_dry_run=ProviderBaselineDryRun(
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            baseline_protocol_digest=protocol_digest if controls_passed else None,
            pair_keys=pair_keys,
            planned_provider_calls=len(pair_keys),
            projected_max_known_cost_usd=1.0,
            projected_max_estimated_cost_usd=1.0,
        ),
        status=status,
        search_authorized=False,
        reason_codes=(
            ("real_provider_baseline_not_run",)
            if controls_passed
            else ("offline_controls_failed",)
        ),
    )


def _product_d0_environment_id(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    source: Path,
) -> str:
    backend, _recorder = _d0_isolated_backend()
    contract = _d0_contract(epoch, task, source, backend)
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
        timeout_s=5.0,
    )
    return f"evaluator.{repo_patch_environment_digest(fixture, backend)[:24]}"


def _product_d0_manifest(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    source: Path,
    protocol_digest: str,
    force_failure: bool,
) -> DevelopmentTaskFeasibilityManifest:
    backend, _recorder = _d0_isolated_backend()
    contract = _d0_contract(epoch, task, source, backend)
    return DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch={} if force_failure else _d0_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_d0_wrong_patch()],
        baseline_protocol_digest=protocol_digest,
    )


def _credential_reference(
    profile: HarnessDeploymentProfile,
) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=profile.endpoint.api_key_env,
        api_key_file_env=profile.endpoint.api_key_file_env,
    )


def _fake_d0_live_proof(
    *,
    manifest: DevelopmentTaskFeasibilityManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    source: Path,
    profile: HarnessDeploymentProfile,
    protocol: HarnessProtocol,
    dependencies: RuntimeDependencyManifest,
) -> D0LiveBaselineProof:
    backend, _recorder = _d0_isolated_backend()
    contract = _d0_contract(epoch, task, source, backend)
    authorization = require_d0_live_authorization(
        feasibility_manifest=manifest,
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        deployment_profile=profile,
        baseline_protocol_digest=protocol.source_digest(),
        credential_reference=_credential_reference(profile),
        live_authorized=True,
    )
    plan = compile_composite_run_plan(task, protocol, dependencies)
    request = HarnessEvaluationRequest(
        evaluation_id=f"evaluation.d0-live.{task.task_manifest_id}",
        arm_id=f"d0-live.{task.task_manifest_id}",
        arm_kind="search_parent",
        control_kind=None,
        opportunity_index=0,
        protocol=protocol,
        compiled_plans=(
            CompiledTaskPlan(
                task_manifest_id=task.task_manifest_id,
                task_manifest_digest=task.task_manifest_digest,
                plan=plan,
            ),
        ),
        expected_pair_keys=authorization.pair_keys,
        deployment_profile_digest=authorization.profile_digest,
        execution_mode="live_provider",
        live_authorization_digest=authorization.authorization_digest,
    )
    receipts = []
    for pair_key in request.expected_pair_keys:
        raw_receipt = _receipt(
            request=request,
            pair_key=pair_key,
            epoch=epoch,
            task=task,
            dependencies=dependencies,
            complete_repair=pair_key.sampling_replicate == 0,
            live=True,
        )
        receipt_payload = raw_receipt.model_dump(
            mode="python",
            exclude={"receipt_digest"},
        )
        receipt_payload.update(
            {
                "evaluation_contract_id": authorization.evaluation_contract_id,
                "evaluation_contract_digest": (
                    authorization.evaluation_contract_digest
                ),
            }
        )
        receipts.append(type(raw_receipt).model_validate(receipt_payload))
    receipts = tuple(receipts)
    headroom = BaselineHeadroomAssessment(
        status="has_headroom",
        receipt_count=len(receipts),
        complete_repairs=sum(receipt.complete_repair for receipt in receipts),
        failures=sum(not receipt.complete_repair for receipt in receipts),
        protocol_digest=protocol.source_digest(),
        receipt_digests=tuple(sorted(receipt.receipt_digest for receipt in receipts)),
        mean_model_calls=sum(receipt.cost.model_calls for receipt in receipts)
        / len(receipts),
        mean_wall_time_ms=sum(receipt.cost.wall_time_ms for receipt in receipts)
        / len(receipts),
        mean_known_cost_usd=sum(
            receipt.cost.known_cost_usd for receipt in receipts
        )
        / len(receipts),
        mean_estimated_cost_usd=sum(
            receipt.cost.estimated_cost_usd for receipt in receipts
        )
        / len(receipts),
    )
    report = D0LiveBaselineReport(
        execution_id=f"d0-live.{task.task_manifest_id}",
        authorization_digest=authorization.authorization_digest,
        provider_dry_run_digest=authorization.provider_dry_run_digest,
        evaluation_contract_authority_digest=(
            d0_evaluation_contract_authority_digest(
                evaluation_contract_id=authorization.evaluation_contract_id,
                evaluation_contract_digest=authorization.evaluation_contract_digest,
            )
        ),
        live_inference_status="completed",
        status="completed",
        scheduled_pair_keys=authorization.pair_keys,
        call_observation_digests=tuple(
            _digest(f"d0-live-observation:{task.task_manifest_id}:{index}")
            for index in range(len(receipts))
        ),
        outcome_receipts=receipts,
        completed_call_count=len(receipts),
        real_inference_requests_sent=sum(
            receipt.real_inference_requests_sent for receipt in receipts
        ),
        total_model_calls=sum(receipt.cost.model_calls for receipt in receipts),
        total_input_tokens=sum(receipt.cost.input_tokens for receipt in receipts),
        total_output_tokens=sum(receipt.cost.output_tokens for receipt in receipts),
        total_cached_tokens=sum(receipt.cost.cached_tokens for receipt in receipts),
        total_known_cost_usd=sum(
            receipt.cost.known_cost_usd for receipt in receipts
        ),
        total_estimated_cost_usd=sum(
            receipt.cost.estimated_cost_usd for receipt in receipts
        ),
        unknown_usage_event_count=0,
        unknown_cost_event_count=0,
        baseline_headroom=headroom,
    )
    return d0_live_baseline_public_proof(
        report=report,
        authorization=authorization,
    )


def _fake_gate0_live_evidence(
    *,
    manifest,
    profile: HarnessDeploymentProfile,
    root: Path,
):
    credential_reference = _credential_reference(profile)
    authorization = require_gate0_live_authorization(
        manifest,
        deployment_profile=profile,
        live_authorized=True,
        credential_reference=credential_reference,
    )
    prior = os.environ.get(GATE0_LIVE_ENABLE_ENV)
    os.environ[GATE0_LIVE_ENABLE_ENV] = "1"
    try:
        report = run_gate0_live(
            manifest=manifest,
            executor=Gate0ScriptedExecutor(
                manifest,
                expected_deployment_profile=profile,
                expected_credential_reference=credential_reference,
            ),
            evidence_root=(
                root / f"gate0-live-{uuid.uuid4().hex}"
            ),
            authorization=authorization,
            live_execution_marker="live_gate0",
        )
    finally:
        if prior is None:
            os.environ.pop(GATE0_LIVE_ENABLE_ENV, None)
        else:
            os.environ[GATE0_LIVE_ENABLE_ENV] = prior
    return authorization, report


def _config(
    *,
    pair_keys: tuple[PairKey, ...],
    controls,
    deployment_profile_digest: str,
    mode: str = "offline_scripted",
    epoch: ResearchEpochManifest | None = None,
) -> PairedHarnessSearchConfig:
    live_authorization = None
    if mode == "live_provider":
        if epoch is None:
            raise ValueError("live factory S1 config requires epoch")
        live_authorization = LiveSearchAuthorization(
            authorization_id="live-search.factory.service",
            search_id="search.factory.service",
            epoch_id=epoch.epoch_id,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            deployment_profile_digest=deployment_profile_digest,
            provider_config_digest=epoch.deployment.provider_config_digest,
            authorized_by="factory-service-test",
        )
    return PairedHarnessSearchConfig(
        search_id="search.factory.service",
        execution_mode=mode,
        deployment_profile_digest=deployment_profile_digest,
        expected_pair_keys=pair_keys,
        live_authorization=live_authorization,
        controls=controls,
        control_opportunities_per_arm=1,
    )


def _build_input(
    tmp_path: Path,
    *,
    mode: str = "offline_scripted",
    prompt: str = "Assemble structural harness release.",
    d0_status: str = "pending_real_provider_baseline",
    gate0_passed: bool = True,
    deployment_profile: HarnessDeploymentProfile | None = None,
    dependencies: RuntimeDependencyManifest | None = None,
    founding_protocol: HarnessProtocol | None = None,
    task_ids: tuple[str, ...] = ("task.search.1",),
    pilot_task_digest: str | None = None,
    expected_parent_message_id: str | None = None,
    expected_message_index: int | None = None,
) -> tuple[HarnessFactoryBuildInput, TaskEnvelope, RuntimeDependencyManifest]:
    dependencies = dependencies or _dependencies()
    effective_profile = deployment_profile or _deployment_profile()
    provider = _provider_identity(effective_profile)
    pair_keys = _pair_keys(_deployment_profile().provider_config_digest, task_ids=task_ids)
    epoch = _epoch(dependencies, pair_keys)
    task_sources = []
    for index, _task_id in enumerate(task_ids, start=1):
        source = tmp_path / "d0_sources" / f"task-{index}"
        if not source.exists():
            _d0_write_source(source)
        task_sources.append(source)
    tasks = tuple(
        _task(
            epoch,
            task_manifest_id=task_id,
            index=index,
            source=task_sources[index - 1],
        )
        for index, task_id in enumerate(task_ids, start=1)
    )
    environment_ids = {
        task.task_manifest_id: _product_d0_environment_id(
            epoch=epoch,
            task=task,
            source=task_sources[index],
        )
        for index, task in enumerate(tasks)
    }
    pair_keys = canonical_pair_keys(
        tuple(
            PairKey(
                task_manifest_id=task_id,
                environment_id=environment_ids[task_id],
                sampling_replicate=replicate,
                provider_config_digest=epoch.deployment.provider_config_digest,
            )
            for task_id in task_ids
            for replicate in range(epoch.search_envelope.sampling_replicates)
        )
    )
    epoch = _epoch(dependencies, pair_keys)
    tasks = tuple(
        _task(
            epoch,
            task_manifest_id=task_id,
            index=index,
            source=task_sources[index - 1],
        )
        for index, task_id in enumerate(task_ids, start=1)
    )
    parent = founding_protocol or load_canonical_harness_seed().protocol
    controls = _controls(parent, tasks[0], dependencies)
    gate0_manifest = build_gate0_dry_run_manifest(
        provider_identity=provider,
        evidence_destination="controlled/gate0-preregistration.json",
    )
    gate0_report = validate_gate0_dry_run_conformance(gate0_manifest)
    if not gate0_passed:
        gate0_report = gate0_report.model_copy(update={"passed": False})
    d0_manifests = tuple(
        _product_d0_manifest(
            epoch=epoch,
            task=task,
            source=task_sources[index],
            protocol_digest=parent.source_digest(),
            force_failure=d0_status == "fail",
        )
        for index, task in enumerate(tasks)
    )
    gate0_live_authorization = None
    gate0_execution_report = None
    d0_live_proofs: tuple[D0LiveBaselineProof, ...] = ()
    if mode == "live_provider":
        gate0_live_authorization, gate0_execution_report = (
            _fake_gate0_live_evidence(
                manifest=gate0_manifest,
                profile=effective_profile,
                root=tmp_path,
            )
        )
        d0_live_proofs = tuple(
            _fake_d0_live_proof(
                manifest=manifest,
                epoch=epoch,
                task=task,
                source=task_sources[index],
                profile=effective_profile,
                protocol=parent,
                dependencies=dependencies,
            )
            for index, (task, manifest) in enumerate(
                zip(tasks, d0_manifests, strict=True)
            )
        )
    return (
        HarnessFactoryBuildInput(
            project_root=str(tmp_path),
            factory_prompt=prompt,
            execution_mode=mode,
            epoch=epoch,
            task_panel=tasks,
            dependency_manifest=dependencies,
            founding_protocol=parent,
            deployment_profile=effective_profile,
            gate0_manifest=gate0_manifest,
            gate0_conformance=gate0_report,
            d0_manifests=d0_manifests,
            gate0_live_authorization=gate0_live_authorization,
            gate0_execution_report=gate0_execution_report,
            d0_live_proofs=d0_live_proofs,
            pilot_summary={
                "pilot_id": "pilot.factory.service",
                "planned_task_manifest_digest": pilot_task_digest
                or _digest("pilot-held-out-task"),
            },
            limitations=(
                (
                    "Live capability authority covers the development search panel; "
                    "the reserved pilot and sealed confirmation have not run."
                )
                if mode == "live_provider"
                else (
                    "Offline scripted factory evidence; no live provider, pilot, "
                    "or sealed-confirmation run."
                )
            ,),
            s1_config=_config(
                pair_keys=pair_keys,
                controls=controls,
                deployment_profile_digest=harness_deployment_profile_digest(
                    effective_profile
                ),
                mode=mode,
                epoch=epoch,
            ),
            chat_id="chat.factory.service",
            expected_parent_message_id=expected_parent_message_id,
            expected_message_index=expected_message_index,
        ),
        tasks[0],
        dependencies,
    )


def _active_release_protocol(root: Path, release_path: str) -> HarnessProtocol:
    return HarnessProtocol.model_validate(
        json.loads((root / release_path / "runtime/harness_protocol.json").read_text(encoding="utf-8"))
    )


def _gain_proposals(*, index: int = 0):
    def proposals(request: ProposalBatchRequest):
        return (_channel_proposal(request, name="gain", index=index),)

    return proposals


def _neutral_proposals(request: ProposalBatchRequest):
    return (_channel_proposal(request, name="neutral", index=0),)


def _prompt_only_proposals(request: ProposalBatchRequest):
    actor = request.incumbent_protocol.actors[0]
    return (
        _proposal(
            transaction_id=f"txn.prompt-only.{request.step_index}",
            parent=request.incumbent_protocol,
            parent_plan=request.incumbent_anchor_plan,
            task=request.anchor_task,
            dependencies=request.dependency_manifest,
            patch=InstructionRewritePatch(
                actor_id=actor.actor_id,
                before_instruction=actor.instruction,
                after_instruction="Use a tighter public-evidence localization instruction.",
            ),
            applicability=TransactionApplicability(required_actor_ids=(actor.actor_id,)),
            touched_source_paths=(f"actors[{actor.actor_id}].instruction",),
        ),
    )


def _release_texts(root: Path, release_path: str) -> str:
    generation = root / release_path
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(generation.rglob("*"))
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}
    )


def test_dry_run_persists_not_run_evidence_without_callbacks_or_release(tmp_path):
    build_input, _task, _dependencies = _build_input(tmp_path, mode="dry_run")

    result = build_harness_factory_release(build_input)

    assert result.execution_mode == "dry_run"
    assert result.live_status == "not_run"
    assert result.release_pointer is None
    assert result.factory_message is None
    assert load_active_release_pointer(tmp_path) is None
    assert not (tmp_path / ".factory_chat").exists()
    evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
    assert evidence["build_digest"] == result.build_digest
    assert evidence["callback_counts"] == {"proposal": 0, "evaluator": 0}
    for key in ("proposal_opportunities", "evaluator_opportunities", "provider_opportunities"):
        assert evidence[key]
        assert {item["status"] for item in evidence[key]} == {"not_run"}
        assert {item["live_status"] for item in evidence[key]} == {"not_run"}
    assert "releases" not in Path(result.evidence_path).parts


def test_dry_run_accepts_multitask_panel_with_exact_d0_pairkey_union(tmp_path):
    build_input, _task, _dependencies = _build_input(
        tmp_path,
        mode="dry_run",
        task_ids=("task.search.1", "task.search.2"),
    )

    result = build_harness_factory_release(build_input)

    assert result.execution_mode == "dry_run"
    assert len(build_input.task_panel) == 2
    assert {d0.task_manifest_id for d0 in build_input.d0_manifests} == {
        task.task_manifest_id for task in build_input.task_panel
    }
    d0_pair_keys = canonical_pair_keys(
        tuple(
            pair
            for d0 in build_input.d0_manifests
            for pair in d0.provider_baseline_dry_run.pair_keys
        )
    )
    assert d0_pair_keys == build_input.s1_config.expected_pair_keys


def test_dry_run_rejects_missing_d0_for_multitask_panel(tmp_path):
    build_input, _task, _dependencies = _build_input(
        tmp_path,
        mode="dry_run",
        task_ids=("task.search.1", "task.search.2"),
    )
    missing = build_input.model_copy(
        update={"d0_manifests": build_input.d0_manifests[:1]}
    )

    with pytest.raises(HarnessFactoryServiceValidationError, match="exactly cover"):
        build_harness_factory_release(missing)


def test_pilot_task_must_be_held_out_from_search_panel(tmp_path):
    build_input, task, _dependencies = _build_input(tmp_path, mode="dry_run")
    reused_pilot = build_input.model_copy(
        update={
            "pilot_summary": build_input.pilot_summary.model_copy(
                update={"planned_task_manifest_digest": task.task_manifest_digest}
            )
        }
    )

    with pytest.raises(HarnessFactoryServiceValidationError, match="held out"):
        build_harness_factory_release(reused_pilot)


def test_offline_initial_publish_commits_public_release_without_leaks(tmp_path):
    build_input, task, dependencies = _build_input(tmp_path)
    calls = []

    result = build_harness_factory_release(
        build_input,
        proposal_callback=_gain_proposals(index=0),
        evaluator_callback=_scripted_evaluator(
            epoch=build_input.epoch,
            task=task,
            dependencies=dependencies,
            calls=calls,
        ),
    )

    assert result.execution_mode == "offline_scripted"
    assert result.search_feasibility_status == "search_viable"
    assert result.release_pointer is not None
    assert result.factory_message is not None
    assert result.factory_message.message_index == 0
    assert result.release_pointer.release_digest == result.factory_message.new_release_digest
    assert len([call for call in calls if call.arm_kind == "search_child"]) == 1
    release_text = _release_texts(tmp_path, result.release_pointer.release_path)
    assert "SEALED_CANARY_VALUE" not in release_text
    assert "sk-test-secret-value" not in release_text
    assert "child_receipts" not in release_text
    assert "founding_receipts" not in release_text
    assert "OutcomeReceipt" not in release_text
    evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
    assert evidence["retained_structural_descendant_count"] == 1
    assert "receipt" not in json.dumps(evidence, sort_keys=True).casefold()


def test_followup_is_serial_reuses_identity_and_preserves_old_release(tmp_path):
    initial_input, task, dependencies = _build_input(tmp_path)
    initial = build_harness_factory_release(
        initial_input,
        proposal_callback=_gain_proposals(index=0),
        evaluator_callback=_scripted_evaluator(
            epoch=initial_input.epoch,
            task=task,
            dependencies=dependencies,
        ),
    )
    followup_input, task, dependencies = _build_input(
        tmp_path,
        prompt="Refine structural handoff.",
        founding_protocol=_active_release_protocol(
            tmp_path,
            initial.release_pointer.release_path,
        ),
        expected_parent_message_id=initial.factory_message.message_id,
        expected_message_index=1,
    )

    followup = build_harness_factory_release(
        followup_input,
        proposal_callback=_gain_proposals(index=1),
        evaluator_callback=_scripted_evaluator(
            epoch=followup_input.epoch,
            task=task,
            dependencies=dependencies,
        ),
    )

    assert initial.release_pointer.release_digest != followup.release_pointer.release_digest
    assert (tmp_path / initial.release_pointer.release_path).is_dir()
    assert (tmp_path / followup.release_pointer.release_path).is_dir()
    assert followup.factory_message.message_index == 1
    assert followup.factory_message.parent_message_id == initial.factory_message.message_id
    assert followup.factory_message.prior_active_release_digest == initial.release_pointer.release_digest
    assert load_active_release_pointer(tmp_path).release_digest == followup.release_pointer.release_digest


def test_offline_requires_explicit_scripted_callbacks(tmp_path):
    build_input, _task, _dependencies = _build_input(tmp_path)

    with pytest.raises(HarnessFactoryExecutionModeError, match="require explicit scripted callbacks"):
        build_harness_factory_release(build_input)


def test_offline_refuses_prompt_only_retention_even_when_outcome_improves(tmp_path):
    build_input, task, dependencies = _build_input(tmp_path)

    with pytest.raises(HarnessFactoryServiceValidationError, match="non-prompt"):
        build_harness_factory_release(
            build_input,
            proposal_callback=_prompt_only_proposals,
            evaluator_callback=_scripted_evaluator(
                epoch=build_input.epoch,
                task=task,
                dependencies=dependencies,
                child_mode="fail_bad_promote_gain",
            ),
        )


def test_offline_refuses_no_gain_search_result(tmp_path):
    build_input, task, dependencies = _build_input(tmp_path)

    with pytest.raises(HarnessFactoryServiceValidationError, match="outcome-improving"):
        build_harness_factory_release(
            build_input,
            proposal_callback=_neutral_proposals,
            evaluator_callback=_scripted_evaluator(
                epoch=build_input.epoch,
                task=task,
                dependencies=dependencies,
                child_mode="equivalent",
            ),
        )


@pytest.mark.parametrize("broken", ["d0", "gate0", "tool_authority", "profile"])
def test_rejects_d0_gate0_and_authority_mismatches(tmp_path, broken):
    kwargs = {}
    if broken == "d0":
        kwargs["d0_status"] = "fail"
    elif broken == "gate0":
        kwargs["gate0_passed"] = False
    elif broken == "tool_authority":
        dependencies = _dependencies()
        tampered_tool = dependencies.trusted_tools[0].model_copy(
            update={"implementation_digest": _digest("tampered-tool")}
        )
        kwargs["dependencies"] = dependencies.model_copy(
            update={"trusted_tools": (tampered_tool,) + dependencies.trusted_tools[1:]}
        )
    elif broken == "profile":
        kwargs["deployment_profile"] = _variant_deployment_profile(provider="other-provider")
        with pytest.raises(ValidationError, match="deployment identity"):
            _build_input(tmp_path, **kwargs)
        return
    build_input, task, dependencies = _build_input(tmp_path, **kwargs)
    if broken == "tool_authority":
        original = _dependencies()
        tampered_tool = original.trusted_tools[0].model_copy(
            update={"implementation_digest": _digest("second-tampered-tool")}
        )
        dependencies = original.model_copy(
            update={"trusted_tools": (tampered_tool,) + original.trusted_tools[1:]}
        )
        build_input = build_input.model_copy(update={"dependency_manifest": dependencies})

    with pytest.raises(HarnessFactoryServiceValidationError):
        build_harness_factory_release(
            build_input,
            proposal_callback=_gain_proposals(index=0),
            evaluator_callback=_scripted_evaluator(
                epoch=build_input.epoch,
                task=task,
                dependencies=dependencies,
            ),
        )


def test_prompt_cannot_change_provider_model_profile_or_carry_secrets(tmp_path):
    with pytest.raises(ValidationError, match="provider"):
        _build_input(tmp_path, prompt="Switch provider before assembling the release.")
    with pytest.raises(ValidationError, match="credential"):
        _build_input(tmp_path, prompt="Assemble release with sk-test-secret-value.")


def test_live_factory_requires_exact_gate0_d0_and_s1_authority_before_release(
    tmp_path: Path,
) -> None:
    build_input, task, dependencies = _build_input(
        tmp_path,
        mode="live_provider",
    )
    proposal_callback = _gain_proposals(index=0)
    evaluator_callback = _scripted_evaluator(
        epoch=build_input.epoch,
        task=task,
        dependencies=dependencies,
        live=True,
    )

    with pytest.raises(HarnessFactoryExecutionModeError, match="explicit"):
        build_harness_factory_release(build_input)

    crossed_gate0 = build_input.model_copy(
        update={
            "gate0_execution_report": build_input.gate0_execution_report.model_copy(
                update={"authorization_digest": _digest("crossed-gate0-authorization")}
            )
        }
    )
    with pytest.raises(HarnessFactoryServiceValidationError, match="Gate0 live execution"):
        build_harness_factory_release(
            crossed_gate0,
            proposal_callback=proposal_callback,
            evaluator_callback=evaluator_callback,
        )

    crossed_d0 = build_input.model_copy(
        update={
            "d0_live_proofs": (
                build_input.d0_live_proofs[0].model_copy(
                    update={
                        "evaluation_contract_authority_digest": _digest(
                            "crossed-d0-evaluation-contract"
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(HarnessFactoryServiceValidationError, match="D0 live proof crossed"):
        build_harness_factory_release(
            crossed_d0,
            proposal_callback=proposal_callback,
            evaluator_callback=evaluator_callback,
        )

    result = build_harness_factory_release(
        build_input,
        proposal_callback=proposal_callback,
        evaluator_callback=evaluator_callback,
    )

    assert result.execution_mode == "live_provider"
    assert result.live_status == "completed"
    assert result.release_pointer is not None
    assert result.live_build_evidence is not None
    assert result.live_build_evidence.capability_promotion_authorized is True
    assert result.live_build_evidence.gate0_real_inference_requests_sent > 0
    assert result.live_build_evidence.d0_real_inference_requests_sent > 0
    assert result.live_build_evidence.search_real_inference_requests_sent > 0
    release = tmp_path / result.release_pointer.release_path
    release_manifest = json.loads(
        (release / "public_release_evidence/release_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    capability = json.loads(
        (release / "public_release_evidence/search/capability_authority.json").read_text(
            encoding="utf-8"
        )
    )
    gate0 = json.loads(
        (release / "public_release_evidence/gate0_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert release_manifest["gate0_status"] == "completed"
    assert capability["search_execution_mode"] == "live_provider"
    assert capability["capability_promotion_authorized"] is True
    assert gate0["status"] == "completed"
    assert gate0["authorization_digest"] == (
        build_input.gate0_live_authorization.authorization_digest
    )
    assert gate0["profile_digest"] == harness_deployment_profile_digest(
        build_input.deployment_profile
    )


def test_followup_rejects_profile_drift_before_transaction(tmp_path):
    initial_input, task, dependencies = _build_input(tmp_path)
    initial = build_harness_factory_release(
        initial_input,
        proposal_callback=_gain_proposals(index=0),
        evaluator_callback=_scripted_evaluator(
            epoch=initial_input.epoch,
            task=task,
            dependencies=dependencies,
        ),
    )
    with pytest.raises(ValidationError, match="deployment identity"):
        _build_input(
            tmp_path,
            prompt="Refine structural handoff.",
            founding_protocol=_active_release_protocol(
                tmp_path,
                initial.release_pointer.release_path,
            ),
            deployment_profile=_variant_deployment_profile(model="other-model"),
            expected_parent_message_id=initial.factory_message.message_id,
            expected_message_index=1,
        )


def test_followup_rejects_founding_protocol_that_is_not_active_release(tmp_path):
    initial_input, task, dependencies = _build_input(tmp_path)
    initial = build_harness_factory_release(
        initial_input,
        proposal_callback=_gain_proposals(index=0),
        evaluator_callback=_scripted_evaluator(
            epoch=initial_input.epoch,
            task=task,
            dependencies=dependencies,
        ),
    )
    stale_seed = load_canonical_harness_seed().protocol
    crossed, task, dependencies = _build_input(
        tmp_path,
        prompt="Refine structural handoff.",
        founding_protocol=stale_seed,
        expected_parent_message_id=initial.factory_message.message_id,
        expected_message_index=1,
    )

    assert stale_seed.source_digest() != _active_release_protocol(
        tmp_path,
        initial.release_pointer.release_path,
    ).source_digest()
    with pytest.raises(HarnessFactoryServiceValidationError, match="founding protocol"):
        build_harness_factory_release(
            crossed,
            proposal_callback=_gain_proposals(index=1),
            evaluator_callback=_scripted_evaluator(
                epoch=crossed.epoch,
                task=task,
                dependencies=dependencies,
            ),
        )
