from __future__ import annotations

import json
import threading
import time
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
from agintor.contracts.feasibility import (
    D0LiveBaselineProof,
    d0_evaluation_contract_authority_digest,
)
from agintor.contracts.outcomes import OutcomeCost, OutcomeHealth, OutcomeReceipt, PairKey
from agintor.core.identity import canonical_identity_digest
from agintor.evaluation.contracts import (
    EvaluationContract,
    HiddenCheck,
    SealedCanary,
    SealedFixtureRef,
    issue_outcome_receipt,
)
from agintor.evaluation.feasibility import (
    D0_LIVE_ENABLE_ENV,
    D0LiveBaselineCallResult,
    D0LiveCallAccounting,
    D0LiveExecutionBlocked,
    DevelopmentTaskFeasibilityRunner,
    d0_live_baseline_public_proof,
    replay_d0_live_provider_baseline,
    require_d0_live_authorization,
    run_d0_live_provider_baseline,
)
from agintor.evaluation.runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    TrustedLocalRepoPatchCommandBackend,
)
from agintor.evaluation.runners.repo_patch_runner import (
    RepoPatchFixture,
    environment_digest,
    repo_patch_fixture_digest,
    repo_snapshot_digest,
)
from agintor.isolation.commands import IsolatedCommandRequest, IsolatedCommandResult
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)
from agintor.runtime.kernel.composite_provider import CredentialReference


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-d0")


class RecordingBackend:
    def __init__(self) -> None:
        self.requests: list[IsolatedCommandRequest] = []
        self._delegate = TrustedLocalRepoPatchCommandBackend()

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self.requests.append(request)
        return self._delegate.run(request)


def _isolated_backend() -> tuple[IsolatedRepoPatchCommandBackend, RecordingBackend]:
    recorder = RecordingBackend()
    backend = IsolatedRepoPatchCommandBackend(
        recorder,
        environment_identity={
            "image": f"agintor-repair@sha256:{'d' * 64}",
            "network": "none",
            "user": "65532:65532",
        },
    )
    return backend, recorder


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=4,
        max_input_tokens=10_000,
        max_output_tokens=4_000,
        max_cached_tokens=2_000,
        max_cache_write_tokens=2_000,
        max_tool_calls=20,
        max_tool_output_bytes=50_000,
        max_artifact_bytes=50_000,
        max_patch_bytes=10_000,
        max_retries=1,
        max_wall_time_ms=60_000,
        provider_deadline_ms=20_000,
        max_known_cost_usd=2.0,
        max_estimated_cost_usd=3.0,
    )


def _profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="provider.fixed.d0",
        provider="openai",
        model="fixed-model",
        endpoint=HarnessProviderEndpoint(
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=2_000,
        ),
        price_schedule=HarnessUsdPriceSchedule(
            billing_mode="paid",
            input_usd_per_million_tokens=1.0,
            output_usd_per_million_tokens=2.0,
            cached_input_usd_per_million_tokens=0.5,
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "d" * 64,
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
    return ResearchEpochManifest(
        epoch_id="epoch.d0",
        task_manifest_digest=_digest("task-manifest"),
        development_split_digest=_digest("development"),
        sealed_confirmation_split_digest=_digest("sealed"),
        deployment=_profile().to_deployment_identity(),
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=3,
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
            max_candidate_evaluations=5,
            max_consecutive_non_improving_steps=2,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="evaluator.d0",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _write_source(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("PROTECTED = True\n", encoding="utf-8")


def _task(epoch: ResearchEpochManifest, source: Path, *, data_state: str = "development") -> TaskEnvelope:
    split = (
        epoch.development_split_digest
        if data_state == "development"
        else epoch.sealed_confirmation_split_digest
    )
    return TaskEnvelope(
        task_manifest_id="task.d0.repair",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state=data_state,
        split_manifest_digest=split,
        issue="Repair VALUE while preserving protected tests.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.d0.clean",
            uri=str(source),
            digest=repo_snapshot_digest(source),
            format="directory",
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public",
                argv=(
                    "python",
                    "-c",
                    (
                        "from pathlib import Path\n"
                        "text = Path('src/app.py').read_text(encoding='utf-8')\n"
                        "assert 'VALUE = 2' in text, text\n"
                    ),
                ),
                timeout_ms=5_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _contract(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    source: Path,
    backend: IsolatedRepoPatchCommandBackend,
) -> EvaluationContract:
    def build(fixture_digest: str) -> EvaluationContract:
        return EvaluationContract(
            evaluation_contract_id="evaluation.d0.repair",
            epoch_id=epoch.epoch_id,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            data_state=task.data_state,
            split_manifest_digest=task.split_manifest_digest,
            task_manifest_id=task.task_manifest_id,
            task_manifest_digest=task.task_manifest_digest,
            sealed_fixture=SealedFixtureRef(
                fixture_id="fixture.d0.repair",
                uri=str(source),
                fixture_digest=fixture_digest,
                public_snapshot_digest=task.workspace_snapshot.digest,
            ),
            protected_paths=("tests",),
            hidden_checks=(
                HiddenCheck(
                    check_id="sealed-exact",
                    argv=(
                        "python",
                        "-c",
                        (
                            "from pathlib import Path\n"
                            "text = Path('src/app.py').read_text(encoding='utf-8')\n"
                            "assert text.strip() == 'VALUE = 2', text\n"
                        ),
                    ),
                    timeout_ms=5_000,
                ),
            ),
            outcome_authority=epoch.evaluator_authority,
            canaries=(SealedCanary(canary_id="d0-canary", value="D0-SEALED-CANARY-4a91"),),
        )

    provisional = build("0" * 64)
    provisional_fixture = RepoPatchFixture.from_evaluation_contract(
        provisional,
        public_test_commands=task.public_reproduction,
        timeout_s=5.0,
    )
    return build(repo_patch_fixture_digest(provisional_fixture, backend))


def _good_patch() -> dict[str, object]:
    return {"files": [{"path": "src/app.py", "updated_content": "VALUE = 2\n"}]}


def _wrong_patch() -> dict[str, object]:
    return {
        "files": [
            {
                "path": "src/app.py",
                "updated_content": "VALUE = 2  # passes public but violates exact behavior\n",
            }
        ]
    }


def _cost(*, cached_tokens: int = 0, cache_write_tokens: int = 0) -> OutcomeCost:
    return OutcomeCost(
        model_calls=1,
        input_tokens=800,
        output_tokens=200,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        tool_calls=3,
        tool_output_bytes=1_000,
        artifact_bytes=500,
        patch_bytes=100,
        retries=0,
        wall_time_ms=4_000,
        known_cost_usd=0.1,
        estimated_cost_usd=0.0,
        unknown_dollars=False,
        within_epoch_envelope=True,
    )


def _receipt(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    contract: EvaluationContract,
    backend: IsolatedRepoPatchCommandBackend,
    replicate: int,
    complete: bool,
) -> OutcomeReceipt:
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
        timeout_s=5.0,
    )
    return issue_outcome_receipt(
        contract=contract,
        epoch=epoch,
        task=task,
        receipt_id=f"receipt.d0.{replicate}",
        release_digest=_digest("release"),
        release_manifest_digest=_digest("release-manifest"),
        profile_digest=_digest("profile"),
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        pair_key=PairKey(
            task_manifest_id=task.task_manifest_id,
            environment_id="environment.d0.clean",
            sampling_replicate=replicate,
            provider_config_digest=epoch.deployment.provider_config_digest,
        ),
        protocol_digest=_digest("strong-single-actor"),
        compiler_digest=_digest("compiler"),
        kernel_digest=_digest("kernel"),
        tool_manifest_digest=_digest("tools"),
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=(
            epoch.deployment.command_container_policy_digest
        ),
        evaluator_environment_digest=environment_digest(fixture, backend),
        patch_digest=_digest(f"patch:{replicate}"),
        complete_repair=complete,
        health=OutcomeHealth(
            process_integrity=True,
            no_leakage=True,
            environment_integrity=True,
            evaluator_integrity=True,
            accounting_complete=True,
        ),
        cost=_cost(),
        issued_at_ms=replicate + 1,
    )


def _setup(tmp_path: Path):
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _isolated_backend()
    epoch = _epoch()
    task = _task(epoch, source)
    contract = _contract(epoch, task, source, backend)
    return source, backend, recorder, epoch, task, contract


def test_offline_controls_pass_and_real_provider_headroom_remains_not_run(tmp_path: Path) -> None:
    source, backend, recorder, epoch, task, contract = _setup(tmp_path)

    manifest = DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch=_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_wrong_patch()],
        baseline_protocol_digest=_digest("strong-single-actor"),
    )

    assert manifest.status == "pending_real_provider_baseline"
    assert manifest.search_authorized is False
    assert manifest.offline_controls_passed is True
    assert manifest.clean_replay_reproducible is True
    assert manifest.protected_path_integrity is True
    assert manifest.leakage_integrity is True
    assert manifest.identity_integrity is True
    assert manifest.baseline_headroom.status == "not_measured"
    assert manifest.baseline_headroom.receipt_count == 0
    assert manifest.provider_baseline_dry_run.real_provider_baseline_status == "not_run"
    assert manifest.provider_baseline_dry_run.inference_authorized is False
    assert manifest.provider_baseline_dry_run.planned_provider_calls == 2
    assert [key.sampling_replicate for key in manifest.provider_baseline_dry_run.pair_keys] == [0, 1]
    assert manifest.paired_search_projection.projected_candidate_evaluations == 5
    assert manifest.paired_search_projection.projected_paired_outcome_runs == 20
    assert manifest.paired_search_projection.projected_max_model_calls == 80
    assert manifest.paired_search_projection.fits_frozen_epoch_budget is True
    assert "real_provider_baseline_not_run" in manifest.reason_codes
    assert repo_snapshot_digest(source) == task.workspace_snapshot.digest
    serialized = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    assert contract.canaries[0].value not in serialized
    assert contract.canaries[0].value_digest not in serialized
    assert recorder.requests


def test_mixed_authoritative_baseline_receipts_establish_real_headroom(tmp_path: Path) -> None:
    _source, backend, _recorder, epoch, task, contract = _setup(tmp_path)
    receipts = [
        _receipt(epoch=epoch, task=task, contract=contract, backend=backend, replicate=0, complete=True),
        _receipt(epoch=epoch, task=task, contract=contract, backend=backend, replicate=1, complete=False),
    ]

    manifest = DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch=_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_wrong_patch()],
        baseline_outcome_receipts=receipts,
    )

    assert manifest.status == "pass"
    assert manifest.search_authorized is True
    assert manifest.baseline_headroom.status == "has_headroom"
    assert manifest.baseline_headroom.complete_repairs == 1
    assert manifest.baseline_headroom.failures == 1
    assert manifest.baseline_headroom.mean_wall_time_ms == 4_000
    assert manifest.baseline_headroom.mean_known_cost_usd == 0.1
    assert manifest.provider_baseline_dry_run.real_provider_baseline_status == "not_run"
    assert manifest.provider_baseline_dry_run.baseline_protocol_digest == _digest("strong-single-actor")


@pytest.mark.parametrize(
    ("outcomes", "expected_status", "reason"),
    [
        ((True, True), "saturated", "baseline_saturated_no_headroom"),
        ((False, False), "uniform_failure", "baseline_uniform_failure"),
    ],
)
def test_saturated_or_uniformly_failing_baseline_stops_search(
    tmp_path: Path,
    outcomes: tuple[bool, bool],
    expected_status: str,
    reason: str,
) -> None:
    _source, backend, _recorder, epoch, task, contract = _setup(tmp_path)
    receipts = [
        _receipt(
            epoch=epoch,
            task=task,
            contract=contract,
            backend=backend,
            replicate=index,
            complete=complete,
        )
        for index, complete in enumerate(outcomes)
    ]

    manifest = DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch=_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_wrong_patch()],
        baseline_outcome_receipts=receipts,
    )

    assert manifest.status == "fail"
    assert manifest.search_authorized is False
    assert manifest.baseline_headroom.status == expected_status
    assert reason in manifest.reason_codes


def test_plausible_wrong_control_that_passes_prevents_feasibility(tmp_path: Path) -> None:
    _source, backend, _recorder, epoch, task, contract = _setup(tmp_path)

    manifest = DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch=_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_good_patch()],
    )

    wrong = next(control for control in manifest.controls if control.control_kind == "plausible_wrong")
    assert wrong.observed_complete_repair is True
    assert wrong.passed is False
    assert manifest.offline_controls_passed is False
    assert manifest.status == "fail"


def test_sealed_task_is_refused_before_any_inspection(tmp_path: Path) -> None:
    source, backend, recorder, epoch, _task_dev, contract = _setup(tmp_path)
    sealed_task = _task(epoch, source, data_state="sealed_confirmation")

    with pytest.raises(ValueError, match="permanently assigned to development"):
        DevelopmentTaskFeasibilityRunner(backend).run(
            epoch=epoch,
            task=sealed_task,
            evaluation_contract=contract,
            known_good_patch=_good_patch(),
            empty_patch={},
            plausible_wrong_patches=[_wrong_patch()],
        )

    assert recorder.requests == []


def test_canary_contaminated_control_is_refused_before_backend_execution(tmp_path: Path) -> None:
    _source, backend, recorder, epoch, task, contract = _setup(tmp_path)
    contaminated = {
        "files": [
            {
                "path": "src/app.py",
                "updated_content": f"VALUE = 2  # {contract.canaries[0].value}\n",
            }
        ]
    }

    with pytest.raises(ValueError, match="sealed canary"):
        DevelopmentTaskFeasibilityRunner(backend).run(
            epoch=epoch,
            task=task,
            evaluation_contract=contract,
            known_good_patch=contaminated,
            empty_patch={},
            plausible_wrong_patches=[_wrong_patch()],
        )

    assert recorder.requests == []


def test_trusted_local_backend_cannot_be_used_for_d0() -> None:
    with pytest.raises(ValueError, match="explicit isolated"):
        DevelopmentTaskFeasibilityRunner(TrustedLocalRepoPatchCommandBackend())


def _offline_manifest_for_live(
    *,
    backend: IsolatedRepoPatchCommandBackend,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    contract: EvaluationContract,
):
    return DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch=_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_wrong_patch()],
        baseline_protocol_digest=_digest("strong-single-actor"),
    )


class FakeD0LiveExecutor:
    def __init__(
        self,
        *,
        epoch: ResearchEpochManifest,
        task: TaskEnvelope,
        contract: EvaluationContract,
        backend: IsolatedRepoPatchCommandBackend,
        mode: str = "success",
        cache_write_tokens: int = 0,
    ) -> None:
        self.epoch = epoch
        self.task = task
        self.contract = contract
        self.backend = backend
        self.mode = mode
        self.cache_write_tokens = cache_write_tokens
        self.calls = []
        self.credentials: list[CredentialReference] = []

    def execute(self, request, *, credential_reference):
        self.calls.append(request)
        self.credentials.append(credential_reference)
        fixture = RepoPatchFixture.from_evaluation_contract(
            self.contract,
            public_test_commands=self.task.public_reproduction,
            timeout_s=5.0,
        )
        complete = request.pair_key.sampling_replicate == 0
        receipt = issue_outcome_receipt(
            contract=self.contract,
            epoch=self.epoch,
            task=self.task,
            receipt_id=f"receipt.d0.live.{request.sequence_index}",
            release_digest=_digest("live-release"),
            release_manifest_digest=_digest("live-release-manifest"),
            profile_digest=request.profile_digest,
            execution_mode="live_provider",
            live_inference_status="completed",
            real_inference_requests_sent=1,
            pair_key=request.pair_key,
            protocol_digest=request.baseline_protocol_digest,
            compiler_digest=_digest("compiler"),
            kernel_digest=_digest("kernel"),
            tool_manifest_digest=_digest("tools"),
            provider_config_digest=self.epoch.deployment.provider_config_digest,
            decoding_policy_digest=self.epoch.deployment.decoding_policy_digest,
            price_schedule_digest=self.epoch.deployment.price_schedule_digest,
            command_container_policy_digest=(
                self.epoch.deployment.command_container_policy_digest
            ),
            evaluator_environment_digest=environment_digest(fixture, self.backend),
            patch_digest=_digest(f"live-patch:{request.sequence_index}"),
            complete_repair=complete,
            health=OutcomeHealth(
                process_integrity=True,
                no_leakage=True,
                environment_integrity=True,
                evaluator_integrity=True,
                accounting_complete=True,
            ),
            cost=_cost(cache_write_tokens=self.cache_write_tokens),
            issued_at_ms=request.sequence_index + 100,
        )
        accounting = D0LiveCallAccounting.from_receipt(receipt)
        if self.mode == "accounting" and request.sequence_index == 0:
            payload = accounting.model_dump(mode="python")
            payload["input_tokens"] += 1
            accounting = D0LiveCallAccounting.model_validate(payload)
        return D0LiveBaselineCallResult(
            call_id=(
                "d0-baseline.crossed"
                if self.mode == "crossed" and request.sequence_index == 0
                else request.call_id
            ),
            request_digest=request.request_digest,
            pair_key=request.pair_key,
            status="succeeded",
            response_ids=(f"live-response-{request.sequence_index}",),
            accounting=accounting,
            outcome_receipt=receipt,
        )


def _live_authorization(manifest, epoch, task, contract, *, deadline_ms=120_000):
    return require_d0_live_authorization(
        feasibility_manifest=manifest,
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        deployment_profile=_profile(),
        baseline_protocol_digest=_digest("strong-single-actor"),
        credential_reference=CredentialReference(
            provider_name="openai",
            api_key_env="OPENAI_API_KEY",
        ),
        call_deadline_ms=deadline_ms,
        live_authorized=True,
    )


def test_d0_live_accounting_rejects_cache_subcategories_above_input_tokens() -> None:
    with pytest.raises(ValidationError, match="cache-write tokens"):
        D0LiveCallAccounting(
            live_inference_status="completed",
            request_sent=True,
            real_inference_requests_sent=1,
            usage_known=True,
            cost_known=True,
            model_calls=1,
            input_tokens=4,
            output_tokens=1,
            cached_tokens=3,
            cache_write_tokens=2,
            known_cost_usd=0.1,
            estimated_cost_usd=0.0,
        )


def test_d0_live_runner_is_blocked_before_executor_without_marker_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, backend, _recorder, epoch, task, contract = _setup(tmp_path)
    manifest = _offline_manifest_for_live(
        backend=backend,
        epoch=epoch,
        task=task,
        contract=contract,
    )
    authorization = _live_authorization(manifest, epoch, task, contract)
    executor = FakeD0LiveExecutor(
        epoch=epoch,
        task=task,
        contract=contract,
        backend=backend,
    )

    monkeypatch.delenv(D0_LIVE_ENABLE_ENV, raising=False)
    with pytest.raises(D0LiveExecutionBlocked, match=D0_LIVE_ENABLE_ENV):
        run_d0_live_provider_baseline(
            feasibility_manifest=manifest,
            epoch=epoch,
            task=task,
            evaluation_contract=contract,
            executor=executor,
            authorization=authorization,
            evidence_root=tmp_path / "d0-live-disabled",
            live_execution_marker="live_d0",
        )
    monkeypatch.setenv(D0_LIVE_ENABLE_ENV, "1")
    with pytest.raises(D0LiveExecutionBlocked, match="marker"):
        run_d0_live_provider_baseline(
            feasibility_manifest=manifest,
            epoch=epoch,
            task=task,
            evaluation_contract=contract,
            executor=executor,
            authorization=authorization,
            evidence_root=tmp_path / "d0-live-wrong-marker",
            live_execution_marker="wrong",  # type: ignore[arg-type]
        )
    crossed_payload = authorization.model_dump(mode="python")
    crossed_payload.pop("authorization_digest", None)
    crossed_payload["provider_dry_run_digest"] = "0" * 64
    crossed = type(authorization).model_validate(crossed_payload)
    with pytest.raises(D0LiveExecutionBlocked, match="crossed"):
        run_d0_live_provider_baseline(
            feasibility_manifest=manifest,
            epoch=epoch,
            task=task,
            evaluation_contract=contract,
            executor=executor,
            authorization=crossed,
            evidence_root=tmp_path / "d0-live-crossed",
            live_execution_marker="live_d0",
        )

    assert executor.calls == []
    assert getattr(run_d0_live_provider_baseline, "live_d0_only") is True
    assert manifest.provider_baseline_dry_run.real_provider_baseline_status == "not_run"


def test_d0_live_fake_executor_persists_replays_exact_pairs_and_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backend, _recorder, epoch, task, contract = _setup(tmp_path)
    manifest = _offline_manifest_for_live(
        backend=backend,
        epoch=epoch,
        task=task,
        contract=contract,
    )
    authorization = _live_authorization(manifest, epoch, task, contract)
    executor = FakeD0LiveExecutor(
        epoch=epoch,
        task=task,
        contract=contract,
        backend=backend,
        cache_write_tokens=5,
    )
    evidence_root = tmp_path / "controlled-d0-live"
    monkeypatch.setenv(D0_LIVE_ENABLE_ENV, "1")

    report = run_d0_live_provider_baseline(
        feasibility_manifest=manifest,
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        executor=executor,
        authorization=authorization,
        evidence_root=evidence_root,
        live_execution_marker="live_d0",
    )

    assert report.status == "completed"
    assert report.live_inference_status == "completed"
    assert report.scheduled_pair_keys == authorization.pair_keys
    assert tuple(call.pair_key for call in executor.calls) == authorization.pair_keys
    assert report.completed_call_count == len(authorization.pair_keys) == 2
    assert report.real_inference_requests_sent == 2
    assert report.total_model_calls == 2
    assert report.total_cache_write_tokens == 10
    assert report.unknown_usage_event_count == 0
    assert report.unknown_cost_event_count == 0
    assert report.baseline_headroom is not None
    assert report.baseline_headroom.status == "has_headroom"
    proof = d0_live_baseline_public_proof(
        report=report,
        authorization=authorization,
    )
    persisted_proof = D0LiveBaselineProof.model_validate_json(
        (evidence_root / "public_proof.json").read_text(encoding="utf-8")
    )
    assert persisted_proof == proof
    assert proof.epoch_id == epoch.epoch_id
    assert proof.epoch_manifest_digest == epoch.epoch_manifest_digest
    assert proof.task_manifest_id == task.task_manifest_id
    assert proof.task_manifest_digest == task.task_manifest_digest
    assert proof.profile_digest == authorization.profile_digest
    assert proof.provider_config_digest == epoch.deployment.provider_config_digest
    assert proof.decoding_policy_digest == epoch.deployment.decoding_policy_digest
    assert proof.price_schedule_digest == epoch.deployment.price_schedule_digest
    assert (
        proof.command_container_policy_digest
        == epoch.deployment.command_container_policy_digest
    )
    assert proof.pair_keys == authorization.pair_keys
    assert proof.scheduled_pair_count == proof.receipt_count == 2
    assert proof.complete_repairs == proof.failures == 1
    assert proof.real_inference_requests_sent == proof.total_model_calls == 2
    assert proof.unknown_usage_event_count == proof.unknown_cost_event_count == 0
    assert proof.authorization_digest == authorization.authorization_digest
    assert proof.report_digest == report.execution_digest
    assert proof.evaluation_contract_authority_digest == (
        d0_evaluation_contract_authority_digest(
            evaluation_contract_id=contract.evaluation_contract_id,
            evaluation_contract_digest=contract.evaluation_contract_digest,
        )
    )
    assert proof.proof_digest
    assert proof.provenance_digest
    public_payload = proof.model_dump(mode="json")
    forbidden_public_fields = {
        "credential_reference",
        "evaluation_contract_id",
        "evaluation_contract_digest",
        "outcome_receipts",
        "response_ids",
        "call_observation_digests",
        "failure",
        "failure_detail",
    }
    assert forbidden_public_fields.isdisjoint(public_payload)
    crossed_payload = proof.model_dump(mode="python")
    crossed_payload["completed_call_count"] += 1
    with pytest.raises(ValueError, match="counts"):
        D0LiveBaselineProof.model_validate(crossed_payload)
    reordered_report_payload = report.model_dump(
        mode="python",
        exclude={"execution_digest"},
    )
    reordered_report_payload["outcome_receipts"] = tuple(
        reversed(reordered_report_payload["outcome_receipts"])
    )
    reordered_report = type(report).model_validate(reordered_report_payload)
    reordered_proof = d0_live_baseline_public_proof(
        report=reordered_report,
        authorization=authorization,
    )
    assert reordered_proof.pair_keys == authorization.pair_keys
    assert reordered_proof.receipt_digests == proof.receipt_digests
    crossed_receipts = []
    for receipt in report.outcome_receipts:
        receipt_payload = receipt.model_dump(
            mode="python",
            exclude={"receipt_digest"},
        )
        receipt_payload["evaluation_contract_id"] = "evaluation.crossed"
        receipt_payload["evaluation_contract_digest"] = _digest(
            "evaluation-contract-crossed"
        )
        crossed_receipts.append(OutcomeReceipt.model_validate(receipt_payload))
    crossed_report_payload = report.model_dump(
        mode="python",
        exclude={"execution_digest"},
    )
    crossed_report_payload["outcome_receipts"] = tuple(crossed_receipts)
    crossed_headroom = report.baseline_headroom.model_dump(mode="python")
    crossed_headroom["receipt_digests"] = tuple(
        sorted(receipt.receipt_digest for receipt in crossed_receipts)
    )
    crossed_report_payload["baseline_headroom"] = crossed_headroom
    crossed_report = type(report).model_validate(crossed_report_payload)
    with pytest.raises(ValueError, match="receipt crossed evaluator or deployment"):
        d0_live_baseline_public_proof(
            report=crossed_report,
            authorization=authorization,
        )
    assert all(
        credential == authorization.credential_reference
        for credential in executor.credentials
    )
    assert replay_d0_live_provider_baseline(
        feasibility_manifest=manifest,
        authorization=authorization,
        evidence_root=evidence_root,
    ) == report
    with pytest.raises(FileExistsError, match="resumeless"):
        run_d0_live_provider_baseline(
            feasibility_manifest=manifest,
            epoch=epoch,
            task=task,
            evaluation_contract=contract,
            executor=executor,
            authorization=authorization,
            evidence_root=evidence_root,
            live_execution_marker="live_d0",
        )

    completed_manifest = DevelopmentTaskFeasibilityRunner(backend).run(
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        known_good_patch=_good_patch(),
        empty_patch={},
        plausible_wrong_patches=[_wrong_patch()],
        baseline_outcome_receipts=report.outcome_receipts,
    )
    assert completed_manifest.status == "pass"
    assert completed_manifest.search_authorized is True
    assert manifest.provider_baseline_dry_run.real_provider_baseline_status == "not_run"
    assert repo_snapshot_digest(source) == task.workspace_snapshot.digest
    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in evidence_root.rglob("*.json")
    )
    assert contract.canaries[0].value not in serialized
    assert "sk-" not in serialized


@pytest.mark.parametrize(
    ("mode", "failure_code"),
    [("crossed", "crossed_identity"), ("accounting", "accounting_error")],
)
def test_d0_live_runner_persists_crossed_identity_and_accounting_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    failure_code: str,
) -> None:
    _source, backend, _recorder, epoch, task, contract = _setup(tmp_path)
    manifest = _offline_manifest_for_live(
        backend=backend,
        epoch=epoch,
        task=task,
        contract=contract,
    )
    authorization = _live_authorization(manifest, epoch, task, contract)
    executor = FakeD0LiveExecutor(
        epoch=epoch,
        task=task,
        contract=contract,
        backend=backend,
        mode=mode,
    )
    evidence_root = tmp_path / f"d0-live-{mode}"
    monkeypatch.setenv(D0_LIVE_ENABLE_ENV, "1")

    report = run_d0_live_provider_baseline(
        feasibility_manifest=manifest,
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        executor=executor,
        authorization=authorization,
        evidence_root=evidence_root,
        live_execution_marker="live_d0",
    )

    assert report.status == "incomplete"
    assert report.failure is not None
    assert report.failure.failure_code == failure_code
    assert report.outcome_receipts == ()
    assert not (evidence_root / "public_proof.json").exists()
    assert replay_d0_live_provider_baseline(
        feasibility_manifest=manifest,
        authorization=authorization,
        evidence_root=evidence_root,
    ) == report


class HungD0Executor(FakeD0LiveExecutor):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.started = threading.Event()
        self.release = threading.Event()
        self.late_returned = threading.Event()
        self.cancelled: list[str] = []

    def execute(self, request, *, credential_reference):
        self.started.set()
        self.release.wait(timeout=2.0)
        result = super().execute(
            request,
            credential_reference=credential_reference,
        )
        self.late_returned.set()
        return result

    def cancel(self, call_id: str) -> None:
        self.cancelled.append(call_id)


def test_d0_live_runner_supervises_hung_executor_and_ignores_late_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, backend, _recorder, epoch, task, contract = _setup(tmp_path)
    manifest = _offline_manifest_for_live(
        backend=backend,
        epoch=epoch,
        task=task,
        contract=contract,
    )
    authorization = _live_authorization(
        manifest,
        epoch,
        task,
        contract,
        deadline_ms=20,
    )
    executor = HungD0Executor(
        epoch=epoch,
        task=task,
        contract=contract,
        backend=backend,
    )
    evidence_root = tmp_path / "d0-live-hung"
    monkeypatch.setenv(D0_LIVE_ENABLE_ENV, "1")
    started = time.monotonic()

    report = run_d0_live_provider_baseline(
        feasibility_manifest=manifest,
        epoch=epoch,
        task=task,
        evaluation_contract=contract,
        executor=executor,
        authorization=authorization,
        evidence_root=evidence_root,
        live_execution_marker="live_d0",
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert report.status == "incomplete"
    assert report.failure is not None
    assert report.failure.failure_code == "deadline_exceeded"
    assert report.unknown_usage_event_count == 1
    assert report.unknown_cost_event_count == 1
    assert report.total_model_calls == 0
    assert executor.cancelled == [report.failure.call_id]
    report_bytes = (evidence_root / "final_report.json").read_bytes()
    executor.release.set()
    assert executor.late_returned.wait(timeout=0.5)
    assert (evidence_root / "final_report.json").read_bytes() == report_bytes
    persisted = json.loads(next((evidence_root / "calls").glob("*.json")).read_text(encoding="utf-8"))
    assert persisted["result"]["accounting"]["usage_known"] is False
    assert "model_calls" not in persisted["result"]["accounting"]
    assert replay_d0_live_provider_baseline(
        feasibility_manifest=manifest,
        authorization=authorization,
        evidence_root=evidence_root,
    ) == report
