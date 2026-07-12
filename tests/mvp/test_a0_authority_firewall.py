from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.authority.public_tasks import (
    assert_public_payload,
    load_public_task,
    public_task_packet,
    sealed_canary_digest,
    task_envelope_public_projection,
)
from agintor.contracts.epochs import (
    DeploymentIdentity,
    EvaluatorAuthority,
    ResearchEpochManifest,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TaskEnvelope,
    TrustedToolAuthority,
    WorkspaceSnapshotRef,
    PublicReproductionStep,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    require_supported_capability_epoch,
)
from agintor.contracts.outcomes import (
    DiagnosticScore,
    OutcomeCost,
    OutcomeHealth,
    PairKey,
    outcome_receipt_digest,
    pair_key_digest,
)
from agintor.contracts.promotion_proof import (
    EvaluatorOutcomeProofBinding,
    PromotionRunEvidenceProjection,
)
from agintor.core.identity import canonical_identity_digest
from agintor.evaluation.contracts import (
    EvaluationContract,
    HiddenCheck,
    SealedCanary,
    SealedFixtureRef,
    assert_evaluation_contract_bound,
    issue_outcome_receipt,
)
from agintor.search.promotion import (
    PromotionRefusal,
    assert_authoritative_outcome_receipt,
    authorize_paired_capability_promotion,
    authorize_paired_search_retention,
)


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-a0")


def _symlink_or_skip(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable on this platform: {exc}")


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


def _epoch(*, epoch_id: str = "epoch.repair.1") -> ResearchEpochManifest:
    evaluator = EvaluatorAuthority(
        evaluator_id="repair-evaluator.v1",
        evaluator_identity_digest=_digest("evaluator"),
        evaluation_policy_digest=_digest("evaluation-policy"),
    )
    tools = tuple(
        TrustedToolAuthority(
            tool_id=tool_id,
            implementation_digest=_digest(f"tool:{tool_id}"),
            policy_digest=_digest(f"policy:{tool_id}"),
        )
        for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
    )
    return ResearchEpochManifest(
        epoch_id=epoch_id,
        task_manifest_digest=_digest("task-manifest"),
        development_split_digest=_digest("development-split"),
        sealed_confirmation_split_digest=_digest("sealed-confirmation-split"),
        deployment=DeploymentIdentity(
            deployment_id="openai.fixed.repair",
            provider="openai",
            model="fixed-model",
            provider_config_digest=_digest("provider-config"),
            decoding_policy_digest=_digest("decoding-policy"),
            price_schedule_digest=_digest("price-schedule"),
            command_container_policy_digest=_digest("command-container-policy"),
        ),
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=4,
            offspring_per_step=2,
            sampling_replicates=1,
            task_panel_digest=_digest("task-panel"),
        ),
        trusted_tools=tools,
        stop_rule=StopRule(
            max_candidate_evaluations=8,
            max_consecutive_non_improving_steps=3,
        ),
        evaluator_authority=evaluator,
    )


def _task(
    epoch: ResearchEpochManifest,
    *,
    issue: str = "Repair the parser regression and preserve all existing behavior.",
    data_state: str = "development",
) -> TaskEnvelope:
    split_digest = (
        epoch.development_split_digest
        if data_state == "development"
        else epoch.sealed_confirmation_split_digest
    )
    return TaskEnvelope(
        task_manifest_id="task.parser.1",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state=data_state,
        split_manifest_digest=split_digest,
        issue=issue,
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.parser.clean",
            uri="fixtures/public/parser-clean",
            digest=_digest("workspace-snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-regression",
                argv=(sys.executable, "-m", "pytest", "tests/test_parser.py"),
                timeout_ms=30_000,
            ),
        ),
        ceilings=_ceilings(),
    )


@pytest.mark.parametrize("unsupported_format", ("tar", "git_bundle"))
def test_workspace_snapshot_contract_rejects_unimplemented_formats(
    unsupported_format: str,
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceSnapshotRef.model_validate(
            {
                "snapshot_id": "snapshot.unsupported",
                "uri": "fixtures/public/snapshot",
                "digest": _digest("unsupported-snapshot"),
                "format": unsupported_format,
            }
        )


def _evaluation_contract(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    *,
    canary: str = "A0-SEALED-CANARY-7f98d4",
) -> EvaluationContract:
    return EvaluationContract(
        evaluation_contract_id="evaluation.parser.1",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state=task.data_state,
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        sealed_fixture=SealedFixtureRef(
            fixture_id="sealed-fixture.parser.1",
            uri="evaluator-mounts/parser-1",
            fixture_digest=_digest("sealed-fixture"),
            public_snapshot_digest=task.workspace_snapshot.digest,
        ),
        protected_paths=("tests",),
        hidden_checks=(
            HiddenCheck(
                check_id="hidden-regression",
                argv=(sys.executable, "-m", "pytest", "evaluator_tests/test_hidden.py"),
                timeout_ms=30_000,
            ),
        ),
        outcome_authority=epoch.evaluator_authority,
        canaries=(SealedCanary(canary_id="target-location", value=canary),),
    )


def _cost() -> OutcomeCost:
    return OutcomeCost(
        model_calls=2,
        input_tokens=1_000,
        output_tokens=500,
        cached_tokens=0,
        tool_calls=4,
        tool_output_bytes=2_000,
        artifact_bytes=1_000,
        patch_bytes=500,
        retries=0,
        wall_time_ms=5_000,
        known_cost_usd=0.2,
        estimated_cost_usd=0.0,
        unknown_dollars=False,
        within_epoch_envelope=True,
    )


def _health() -> OutcomeHealth:
    return OutcomeHealth(
        process_integrity=True,
        no_leakage=True,
        environment_integrity=True,
        evaluator_integrity=True,
        accounting_complete=True,
    )


def _receipt(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    contract: EvaluationContract,
    receipt_id: str,
    protocol: str,
    complete_repair: bool,
    diagnostics: tuple[DiagnosticScore, ...] = (),
    live: bool = False,
):
    return issue_outcome_receipt(
        contract=contract,
        epoch=epoch,
        task=task,
        receipt_id=receipt_id,
        release_digest=_digest("release"),
        release_manifest_digest=_digest("release-manifest"),
        profile_digest=_digest("profile"),
        execution_mode="live_provider" if live else "deterministic_replay",
        live_inference_status="completed" if live else "not_run",
        real_inference_requests_sent=2 if live else 0,
        pair_key=PairKey(
            task_manifest_id=task.task_manifest_id,
            environment_id="environment.python312.fixture1",
            sampling_replicate=0,
            provider_config_digest=epoch.deployment.provider_config_digest,
        ),
        protocol_digest=_digest(protocol),
        compiler_digest=_digest("compiler"),
        kernel_digest=_digest("kernel"),
        tool_manifest_digest=_digest("tools"),
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=(
            epoch.deployment.command_container_policy_digest
        ),
        evaluator_environment_digest=_digest("environment"),
        patch_digest=_digest(f"patch:{receipt_id}"),
        complete_repair=complete_repair,
        health=_health(),
        cost=_cost(),
        diagnostics=diagnostics,
        issued_at_ms=1,
    )


def _proof_binding(receipt) -> EvaluatorOutcomeProofBinding:
    evidence_digest = _digest(f"run-evidence:{receipt.receipt_id}")
    run = PromotionRunEvidenceProjection(
        evidence_id=f"evidence.{receipt.receipt_id}",
        evidence_digest=evidence_digest,
        run_id=f"run.{receipt.receipt_id}",
        execution_mode=receipt.execution_mode,
        live_inference_status=receipt.live_inference_status,
        real_inference_requests_sent=receipt.real_inference_requests_sent,
        arm="intact",
        capability_epoch=receipt.capability_epoch,
        data_state=receipt.data_state,
        epoch_id=receipt.epoch_id,
        epoch_manifest_digest=receipt.epoch_manifest_digest,
        release_digest=receipt.release_digest,
        release_manifest_digest=receipt.release_manifest_digest,
        profile_digest=receipt.profile_digest,
        split_manifest_digest=receipt.split_manifest_digest,
        pair_key=receipt.pair_key,
        task_manifest_digest=receipt.task_manifest_digest,
        protocol_digest=receipt.protocol_digest,
        compiled_semantic_digest=_digest(f"compiled:{receipt.protocol_digest}"),
        dependency_manifest_digest=_digest("dependencies"),
        compiler_digest=receipt.compiler_digest,
        kernel_digest=receipt.kernel_digest,
        tool_manifest_digest=receipt.tool_manifest_digest,
        provider_config_digest=receipt.provider_config_digest,
        decoding_policy_digest=receipt.decoding_policy_digest,
        price_schedule_digest=receipt.price_schedule_digest,
        command_container_policy_digest=receipt.command_container_policy_digest,
        deployment_id="openai.fixed.repair",
        provider="openai",
        model="fixed-model",
        cost_ledger_digest=_digest(f"cost:{receipt.receipt_id}"),
        runtime_environment_digest=_digest("runtime-environment"),
        patch_digest=receipt.patch_digest,
        healthy=True,
    )
    return EvaluatorOutcomeProofBinding(
        outcome_receipt=receipt,
        proof_record_id=f"proof.{receipt.receipt_id}",
        proof_record_digest=_digest(f"proof:{receipt.receipt_id}"),
        run_evidence=run,
        run_evidence_digest=evidence_digest,
        proof_record_ref=(
            f"runs/{pair_key_digest(receipt.pair_key)}/"
            f"{receipt.protocol_digest}/{evidence_digest}.json"
        ),
        outcome_link_ref=f"outcome_links/{receipt.receipt_digest}.json",
    )


def test_epoch_and_task_contracts_are_strict_digest_bound_and_repo_repair_only() -> None:
    epoch = _epoch()
    task = _task(epoch)

    assert epoch.capability_epoch == "repo-repair-v1"
    assert epoch.data_states == ("development", "sealed_confirmation")
    assert task.allowed_capabilities == REPO_REPAIR_TRUSTED_TOOL_IDS
    assert len(epoch.epoch_manifest_digest) == 64
    assert len(task.task_manifest_digest) == 64

    payload = task.model_dump(mode="json")
    payload["issue"] = "tampered issue"
    with pytest.raises(ValidationError, match="task_manifest_digest"):
        TaskEnvelope.model_validate(payload)

    epoch_payload = epoch.model_dump(mode="json")
    epoch_payload["capability_epoch"] = "generic-agent-v1"
    with pytest.raises(ValidationError):
        ResearchEpochManifest.model_validate(epoch_payload)

    assert require_supported_capability_epoch("repo-repair-v1") == "repo-repair-v1"
    with pytest.raises(ValueError, match="explicit capability epoch"):
        require_supported_capability_epoch(None)
    with pytest.raises(ValueError, match="only repo-repair-v1"):
        require_supported_capability_epoch("generic")


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "target_files",
        "expected_answer",
        "gold_patch",
        "hidden_checks",
        "evaluation_contract",
        "operation_dag",
    ],
)
def test_public_task_loader_rejects_hidden_and_evaluator_fields(
    tmp_path: Path,
    forbidden_field: str,
) -> None:
    epoch = _epoch()
    payload = _task(epoch).model_dump(mode="json")
    payload[forbidden_field] = "must-not-cross"
    path = tmp_path / "public-task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden in a public payload"):
        load_public_task(path, epoch=epoch, audience="runtime")


def test_public_task_loader_refuses_sealed_mount_paths(tmp_path: Path) -> None:
    epoch = _epoch()
    sealed_dir = tmp_path / "sealed"
    sealed_dir.mkdir()
    path = sealed_dir / "public-task.json"
    path.write_text(json.dumps(_task(epoch).model_dump(mode="json")), encoding="utf-8")

    with pytest.raises(ValueError, match="refuses evaluator/sealed source paths"):
        load_public_task(path, epoch=epoch, audience="runtime")


def test_public_task_loader_refuses_reserved_path_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = _epoch()
    sealed_dir = tmp_path / "sealed"
    sealed_dir.mkdir()
    path = sealed_dir / "public-task.json"
    path.write_text("{ this must not be parsed", encoding="utf-8")

    def fail_read(_: Path) -> bytes:
        raise AssertionError("public task loader read sealed bytes before rejecting path")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(ValueError, match="refuses evaluator/sealed source paths"):
        load_public_task(path, epoch=epoch, audience="runtime")


def test_public_task_loader_refuses_symlink_source_file(tmp_path: Path) -> None:
    epoch = _epoch()
    target = tmp_path / "target-task.json"
    target.write_text(json.dumps(_task(epoch).model_dump(mode="json")), encoding="utf-8")
    link = tmp_path / "public-task.json"
    _symlink_or_skip(link, target)

    with pytest.raises(ValueError, match="symlink or junction source paths"):
        load_public_task(link, epoch=epoch, audience="runtime")


def test_public_task_loader_refuses_symlink_parent(
    tmp_path: Path,
) -> None:
    epoch = _epoch()
    real_parent = tmp_path / "real-public"
    real_parent.mkdir()
    (real_parent / "public-task.json").write_text(
        json.dumps(_task(epoch).model_dump(mode="json")),
        encoding="utf-8",
    )
    link_parent = tmp_path / "linked-public"
    _symlink_or_skip(link_parent, real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink or junction source paths"):
        load_public_task(
            link_parent / "public-task.json",
            epoch=epoch,
            audience="runtime",
        )


def test_public_task_loader_rechecks_resolved_reserved_path_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = _epoch()
    public_path = tmp_path / "public" / "public-task.json"
    public_path.parent.mkdir()
    public_path.write_text(
        json.dumps(_task(epoch).model_dump(mode="json")),
        encoding="utf-8",
    )
    resolved_reserved = tmp_path / "sealed" / "public-task.json"
    resolved_reserved.parent.mkdir()
    resolved_reserved.write_text("{ this must not be parsed", encoding="utf-8")

    original_resolve = Path.resolve

    def fake_resolve(self: Path, *args, **kwargs) -> Path:
        if self == public_path:
            return resolved_reserved
        return original_resolve(self, *args, **kwargs)

    def fail_read(_: Path) -> bytes:
        raise AssertionError(
            "public task loader read resolved sealed bytes before rejecting path"
        )

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(ValueError, match="refuses evaluator/sealed source paths"):
        load_public_task(public_path, epoch=epoch, audience="runtime")


def test_public_loader_and_packets_reject_sealed_canaries_at_any_depth(tmp_path: Path) -> None:
    epoch = _epoch()
    canary = "A0-SEALED-CANARY-a83b"
    task = _task(epoch, issue=f"Repair the regression. {canary}")
    path = tmp_path / "public-task.json"
    path.write_text(json.dumps(task.model_dump(mode="json")), encoding="utf-8")

    with pytest.raises(ValueError, match="sealed canary"):
        load_public_task(path, epoch=epoch, audience="runtime", canary_values=(canary,))
    with pytest.raises(ValueError, match="canary digest"):
        load_public_task(
            path,
            epoch=epoch,
            audience="runtime",
            canary_digests=(sealed_canary_digest(task.issue),),
        )
    with pytest.raises(ValueError, match="sealed canary"):
        public_task_packet(task, epoch, audience="proposer", canary_values=(canary,))
    with pytest.raises(ValueError, match="sealed canary"):
        assert_public_payload({"nested": {canary: "value"}}, canary_values=(canary,))


def test_public_projection_is_recursive_allowlist_and_evaluation_contract_is_rejected() -> None:
    epoch = _epoch()
    task = _task(epoch)
    contract = _evaluation_contract(epoch, task)

    projection = task_envelope_public_projection(task)
    assert set(projection) == {
        "runtime_contract_version",
        "task_manifest_id",
        "task_manifest_digest",
        "epoch_id",
        "epoch_manifest_digest",
        "capability_epoch",
        "data_state",
        "split_manifest_digest",
        "issue",
        "workspace_snapshot",
        "public_reproduction",
        "allowed_capabilities",
        "ceilings",
    }
    serialized = json.dumps(projection, sort_keys=True)
    assert contract.canaries[0].value not in serialized
    with pytest.raises(ValueError, match="forbidden in a public payload"):
        assert_public_payload(contract)


def test_public_process_import_path_does_not_load_evaluator_contract_or_sealed_io() -> None:
    script = """
import sys
import agintor.authority.public_tasks
assert 'agintor.evaluation.contracts' not in sys.modules
assert 'agintor.oracle.package_io' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("role", ["factory", "runtime", "proposer"])
def test_public_process_roles_cannot_import_evaluation_contract_or_load_sealed_package(
    role: str,
) -> None:
    import_script = "import agintor.evaluation.contracts"
    env = {**os.environ, "AGINTOR_PROCESS_ROLE": role}
    imported = subprocess.run(
        [sys.executable, "-c", import_script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert imported.returncode != 0
    assert "cannot import evaluator-only EvaluationContract" in imported.stderr

    load_script = """
from agintor.oracle.package_io import load_oracle_package
try:
    load_oracle_package('does-not-matter')
except PermissionError as exc:
    assert 'sealed authority is evaluator-only' in str(exc)
else:
    raise AssertionError('public process loaded a sealed package')
"""
    loaded = subprocess.run(
        [sys.executable, "-c", load_script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert loaded.returncode == 0, loaded.stderr


def test_sealed_confirmation_requires_explicit_confirmation_runner(tmp_path: Path) -> None:
    epoch = _epoch()
    task = _task(epoch, data_state="sealed_confirmation")
    path = tmp_path / "public-confirmation-task.json"
    path.write_text(json.dumps(task.model_dump(mode="json")), encoding="utf-8")

    with pytest.raises(ValueError, match="explicit confirmation-runner"):
        load_public_task(path, epoch=epoch, audience="runtime")
    with pytest.raises(ValueError, match="cannot load"):
        load_public_task(
            path,
            epoch=epoch,
            audience="proposer",
            allow_sealed_confirmation=True,
        )
    loaded = load_public_task(
        path,
        epoch=epoch,
        audience="confirmation_runner",
        allow_sealed_confirmation=True,
    )
    assert loaded.data_state == "sealed_confirmation"


def test_evaluation_contract_binds_epoch_task_fixture_authority_and_canaries() -> None:
    epoch = _epoch()
    task = _task(epoch)
    contract = _evaluation_contract(epoch, task)

    assert_evaluation_contract_bound(contract, epoch=epoch, task=task)
    assert len(contract.evaluation_contract_digest) == 64

    crossed = _epoch(epoch_id="epoch.repair.crossed")
    with pytest.raises(ValueError, match="task epoch_id"):
        assert_evaluation_contract_bound(contract, epoch=crossed, task=task)

    payload = contract.model_dump(mode="json")
    payload["protected_paths"] = ["src"]
    with pytest.raises(ValidationError, match="evaluation_contract_digest"):
        EvaluationContract.model_validate(payload)


def test_only_paired_evaluator_complete_repair_outcomes_authorize_promotion() -> None:
    epoch = _epoch()
    task = _task(epoch)
    contract = _evaluation_contract(epoch, task)
    parent = _receipt(
        epoch=epoch,
        task=task,
        contract=contract,
        receipt_id="receipt.parent",
        protocol="parent",
        complete_repair=False,
        live=True,
    )
    child = _receipt(
        epoch=epoch,
        task=task,
        contract=contract,
        receipt_id="receipt.child",
        protocol="child",
        complete_repair=True,
        live=True,
    )
    parent_proof = _proof_binding(parent)
    child_proof = _proof_binding(child)

    authorization = authorize_paired_capability_promotion(
        epoch=epoch,
        parent_proofs=(parent_proof,),
        child_proofs=(child_proof,),
    )
    assert authorization.decision == "retain_child"
    assert authorization.complete_repair_gain == 1

    with pytest.raises(PromotionRefusal, match="required"):
        authorize_paired_capability_promotion(
            epoch=epoch,
            parent_proofs=(),
            child_proofs=(),
        )
    with pytest.raises(PromotionRefusal, match="proof binding"):
        authorize_paired_capability_promotion(
            epoch=epoch,
            parent_proofs=(parent,),
            child_proofs=(child,),
        )


def test_diagnostic_or_process_perfect_results_cannot_select_a_child() -> None:
    epoch = _epoch()
    task = _task(epoch)
    contract = _evaluation_contract(epoch, task)
    diagnostic = (DiagnosticScore(name="trace_quality", value=1.0),)
    parent = _receipt(
        epoch=epoch,
        task=task,
        contract=contract,
        receipt_id="receipt.parent.diagnostic",
        protocol="parent",
        complete_repair=False,
    )
    child = _receipt(
        epoch=epoch,
        task=task,
        contract=contract,
        receipt_id="receipt.child.diagnostic",
        protocol="child",
        complete_repair=False,
        diagnostics=diagnostic,
    )

    with pytest.raises(PromotionRefusal, match="cannot replace complete-repair"):
        authorize_paired_search_retention(
            epoch=epoch,
            parent_proofs=(_proof_binding(parent),),
            child_proofs=(_proof_binding(child),),
        )


def test_crossed_epoch_evaluator_and_evaluation_contract_digests_fail_closed() -> None:
    epoch = _epoch()
    task = _task(epoch)
    contract = _evaluation_contract(epoch, task)
    parent = _receipt(
        epoch=epoch,
        task=task,
        contract=contract,
        receipt_id="receipt.parent.cross",
        protocol="parent",
        complete_repair=False,
    )
    child = _receipt(
        epoch=epoch,
        task=task,
        contract=contract,
        receipt_id="receipt.child.cross",
        protocol="child",
        complete_repair=True,
    )

    crossed_epoch = child.model_copy(
        update={"epoch_manifest_digest": _digest("crossed-epoch")}
    )
    crossed_epoch = crossed_epoch.model_copy(
        update={"receipt_digest": outcome_receipt_digest(crossed_epoch)}
    )
    with pytest.raises(PromotionRefusal, match="epoch digest crossed"):
        assert_authoritative_outcome_receipt(crossed_epoch, epoch)
    crossed_evaluator = child.model_copy(
        update={"evaluator_identity_digest": _digest("crossed-evaluator")}
    )
    crossed_evaluator = crossed_evaluator.model_copy(
        update={"receipt_digest": outcome_receipt_digest(crossed_evaluator)}
    )
    with pytest.raises(PromotionRefusal, match="evaluator digest crossed"):
        assert_authoritative_outcome_receipt(crossed_evaluator, epoch)
    crossed_child = child.model_copy(
        update={"evaluation_contract_digest": _digest("crossed-contract")}
    )
    crossed_child = crossed_child.model_copy(
        update={"receipt_digest": outcome_receipt_digest(crossed_child)}
    )
    with pytest.raises(PromotionRefusal, match="evaluation_contract_digest"):
        authorize_paired_search_retention(
            epoch=epoch,
            parent_proofs=(_proof_binding(parent),),
            child_proofs=(_proof_binding(crossed_child),),
        )
