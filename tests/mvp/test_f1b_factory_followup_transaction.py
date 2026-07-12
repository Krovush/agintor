from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.core.identity import canonical_identity_digest
from agintor.factory.harness_release import load_active_release_pointer
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
    load_composite_compiler_metadata,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)
from agintor.storage.harness_factory_transaction import (
    HARNESS_FACTORY_CHAT_DIR_NAME,
    HarnessFactoryConcurrencyError,
    HarnessFactoryInjectedFailure,
    HarnessFactoryStaleHeadError,
    HarnessFactoryTransactionStore,
    HarnessFactoryValidationError,
)


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-f1b")


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=8,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cached_tokens=10_000,
        max_tool_calls=20,
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
    tools = tuple(
        TrustedToolDependency(
            tool_id=tool_id,
            interface_version="repo-tool-v1",
            implementation_digest=_digest(f"tool-impl-{tool_id}"),
            policy_digest=_digest(f"tool-policy-{tool_id}"),
        )
        for tool_id in sorted(REPO_REPAIR_TRUSTED_TOOL_IDS)
    )
    return RuntimeDependencyManifest(
        compiler=DependencyRef(
            dependency_id=metadata.compiler_id,
            interface_version=metadata.compiler_version,
            implementation_digest=_digest("compiler-impl"),
        ),
        harness_contract=DependencyRef(
            dependency_id=metadata.harness_contract_id,
            interface_version=metadata.harness_schema_version,
            implementation_digest=_digest("harness-contract-impl"),
        ),
        kernel=DependencyRef(
            dependency_id="agintor.composite_kernel",
            interface_version="kernel-v1",
            implementation_digest=_digest("kernel-impl"),
        ),
        trusted_tools=tools,
    )


def _deployment_profile(*, epoch_label: str) -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="scripted.harness.f1b",
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
            provider_policy_justification=f"scripted fixture {epoch_label} has no provider billing",
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "b" * 64,
            timeout_s=30.0,
            memory_bytes=512 * 1024 * 1024,
            cpu_count=1.0,
            pids_limit=128,
            output_bytes=1_000_000,
            tmpfs_bytes=64 * 1024 * 1024,
            nofile_limit=256,
        ),
    )


def _epoch(dependencies: RuntimeDependencyManifest, *, epoch_label: str) -> ResearchEpochManifest:
    deployment = _deployment_profile(epoch_label=epoch_label).to_deployment_identity()
    return ResearchEpochManifest(
        epoch_id=f"epoch.{epoch_label}",
        task_manifest_digest=_digest(f"{epoch_label}-task-distribution"),
        development_split_digest=_digest(f"{epoch_label}-development"),
        sealed_confirmation_split_digest=_digest(f"{epoch_label}-sealed"),
        deployment=deployment,
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=2,
            offspring_per_step=1,
            sampling_replicates=2,
            task_panel_digest=_digest(f"{epoch_label}-panel"),
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
            max_candidate_evaluations=2,
            max_consecutive_non_improving_steps=3,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="eval.f1b",
            evaluator_identity_digest=_digest(f"{epoch_label}-evaluator"),
            evaluation_policy_digest=_digest(f"{epoch_label}-policy"),
        ),
    )


def _task(epoch: ResearchEpochManifest) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="task.f1b.public",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue="Repair the public failure without sealed hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.f1b",
            uri="cas://snapshot-f1b",
            digest=_digest("snapshot-f1b"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=10_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _release_request(release_label: str, *, epoch_label: str = "same") -> tuple[HarnessReleaseRequest, str, tuple[str, ...]]:
    dependencies = _dependencies()
    epoch = _epoch(dependencies, epoch_label=epoch_label)
    task = _task(epoch)
    protocol = load_canonical_harness_seed().protocol
    plan = compile_composite_run_plan(task, protocol, dependencies)
    protocol_digest = protocol.source_digest()
    selection_evidence = (_digest(f"{release_label}-selection-evidence"),)
    preregistration = Gate0PreregistrationPublic(
        preregistration_id=f"gate0.{release_label}",
        panel_digest=_digest(f"{release_label}-gate0-panel"),
        deterministic_suite_digest=_digest(f"{release_label}-gate0-suite"),
        planned_provider_calls=1280,
        frozen_thresholds={
            "intact_success_min": 0.70,
            "intact_minus_null_min": 0.30,
        },
    )
    request = HarnessReleaseRequest(
        epoch=epoch,
        selected_protocol=protocol,
        representative_plan=plan,
        dependency_manifest=dependencies,
        deployment_profile=_deployment_profile(epoch_label=epoch_label),
        deployment=epoch.deployment,
        search_lineage=(
            PublicSearchLineageRecord(
                sequence_no=0,
                transaction_id=f"txn.{release_label}",
                operator="instruction_rewrite",
                parent_protocol_digest=protocol_digest,
                child_protocol_digest=protocol_digest,
                transaction_digest=_digest(f"{release_label}-semantic-txn"),
                mechanism_hypothesis_digest=_digest(f"{release_label}-hypothesis"),
                status="accepted",
            ),
        ),
        selection_decisions=(
            PublicSelectionDecision(
                sequence_no=0,
                decision_id=f"decision.{release_label}",
                incumbent_protocol_digest=protocol_digest,
                candidate_protocol_digest=protocol_digest,
                selected_protocol_digest=protocol_digest,
                decision="retain_candidate",
                reason_codes=("offline_fixture",),
                evidence_digests=selection_evidence,
            ),
        ),
        gate0_preregistration=preregistration,
        gate0_report=Gate0NotRunReport(
            preregistration_digest=preregistration.preregistration_digest,
        ),
        pilot_summary=PilotNotRunSummary(
            pilot_id=f"pilot.{release_label}",
            planned_task_manifest_digest=task.task_manifest_digest,
        ),
        limitations=(f"Test-ready no-live-inference release {release_label}.",),
    )
    return request, _digest(f"{release_label}-search-result"), selection_evidence


def _publish_initial(
    store: HarnessFactoryTransactionStore,
    *,
    label: str = "initial",
    fail_at: str | None = None,
):
    request, search_digest, selection_evidence = _release_request(label)
    return store.create_initial_chat(
        request=request,
        user_prompt_text=f"Build harness release {label}.",
        chat_id="chat.f1b",
        search_result_digest=search_digest,
        selection_evidence_digests=selection_evidence,
        fail_at=fail_at,
    )


def test_initial_and_followup_atomically_commit_message_and_active_release(tmp_path: Path) -> None:
    store = HarnessFactoryTransactionStore(tmp_path)

    first = _publish_initial(store)
    pointer = load_active_release_pointer(tmp_path)
    chat = store.load_chat()

    assert pointer is not None
    assert pointer.release_digest == first.new_release_digest
    assert chat.active_release_digest == first.new_release_digest
    assert chat.message_count == 1
    assert chat.last_message_id == first.message_id
    assert not (tmp_path / "releases" / first.new_release_digest / HARNESS_FACTORY_CHAT_DIR_NAME).exists()

    request, search_digest, selection_evidence = _release_request("followup")
    second = store.apply_followup(
        request=request,
        user_prompt_text="Prefer lower-cost harness evidence when outcomes are equivalent.",
        expected_parent_message_id=first.message_id,
        expected_message_index=1,
        search_result_digest=search_digest,
        selection_evidence_digests=selection_evidence,
    )

    pointer = load_active_release_pointer(tmp_path)
    chat = store.load_chat()
    messages = store.messages()

    assert pointer is not None
    assert pointer.release_digest == second.new_release_digest
    assert pointer.release_digest != first.new_release_digest
    assert chat.active_release_digest == second.new_release_digest
    assert chat.last_message_id == second.message_id
    assert [message.message_index for message in messages] == [0, 1]
    assert messages[1].prior_active_release_digest == first.new_release_digest
    assert messages[1].parent_message_id == first.message_id
    assert messages[1].new_release_digest == second.new_release_digest
    assert messages[1].new_release_digest != messages[1].prior_active_release_digest


def test_cross_project_message_identity_is_deterministic_and_repeated_followup_rejects(
    tmp_path: Path,
) -> None:
    first_request, first_search, first_selection = _release_request("portable-initial")
    stores = (
        HarnessFactoryTransactionStore(tmp_path / "first-project"),
        HarnessFactoryTransactionStore(tmp_path / "second-project"),
    )
    initial_messages = tuple(
        store.create_initial_chat(
            request=first_request,
            user_prompt_text="Build the portable initial harness release.",
            search_result_digest=first_search,
            selection_evidence_digests=first_selection,
        )
        for store in stores
    )
    assert len({message.chat_id for message in initial_messages}) == 1
    assert len({message.message_id for message in initial_messages}) == 1
    assert len({message.transaction_id for message in initial_messages}) == 1

    follow_request, follow_search, follow_selection = _release_request(
        "portable-followup"
    )
    followup_messages = tuple(
        store.apply_followup(
            request=follow_request,
            user_prompt_text="Add the same portable follow-up evidence.",
            expected_parent_message_id=initial.message_id,
            expected_message_index=1,
            search_result_digest=follow_search,
            selection_evidence_digests=follow_selection,
        )
        for store, initial in zip(stores, initial_messages, strict=True)
    )
    assert len({message.message_id for message in followup_messages}) == 1
    assert len({message.transaction_id for message in followup_messages}) == 1
    with pytest.raises(HarnessFactoryStaleHeadError, match="parent message is stale"):
        stores[0].apply_followup(
            request=follow_request,
            user_prompt_text="Add the same portable follow-up evidence.",
            expected_parent_message_id=initial_messages[0].message_id,
            expected_message_index=1,
            search_result_digest=follow_search,
            selection_evidence_digests=follow_selection,
        )

    tampered = HarnessFactoryTransactionStore(tmp_path / "tampered-project")
    tampered_message = tampered.create_initial_chat(
        request=first_request,
        user_prompt_text="Build a materially different portable harness release.",
        search_result_digest=first_search,
        selection_evidence_digests=first_selection,
    )
    assert tampered_message.message_id != initial_messages[0].message_id
    assert tampered_message.transaction_id != initial_messages[0].transaction_id


@pytest.mark.parametrize(
    ("fail_at", "should_commit"),
    [
        ("after_materialize", False),
        ("after_prepare", False),
        ("after_message_staged", False),
        ("after_commit_intent", True),
        ("after_pointer", True),
        ("after_message", True),
        ("after_chat_manifest", True),
        ("after_committed_marker", True),
    ],
)
def test_recover_handles_every_transaction_boundary(
    tmp_path: Path,
    fail_at: str,
    should_commit: bool,
) -> None:
    store = HarnessFactoryTransactionStore(tmp_path)

    with pytest.raises(HarnessFactoryInjectedFailure, match=fail_at):
        _publish_initial(store, label=f"boundary-{fail_at}", fail_at=fail_at)

    recovered = store.recover()
    pointer = load_active_release_pointer(tmp_path)
    messages = store.messages()

    if should_commit:
        assert recovered is not None
        assert pointer is not None
        assert len(messages) == 1
        assert pointer.release_digest == messages[0].new_release_digest
        assert recovered.active_release_digest == messages[0].new_release_digest
        assert recovered.last_message_id == messages[0].message_id
        second_recovery = store.recover()
        assert second_recovery == recovered
        assert [message.message_digest for message in store.messages()] == [messages[0].message_digest]
    else:
        assert recovered is None
        assert pointer is None
        assert messages == []


def test_followup_commit_intent_recovery_never_leaves_wrong_pointer_or_message(tmp_path: Path) -> None:
    store = HarnessFactoryTransactionStore(tmp_path)
    first = _publish_initial(store)

    request, search_digest, selection_evidence = _release_request("pointer-failure")
    with pytest.raises(HarnessFactoryInjectedFailure, match="after_pointer"):
        store.apply_followup(
            request=request,
            user_prompt_text="Publish the next harness release coherently.",
            expected_parent_message_id=first.message_id,
            search_result_digest=search_digest,
            selection_evidence_digests=selection_evidence,
            fail_at="after_pointer",
        )

    pointer_before = load_active_release_pointer(tmp_path)
    assert pointer_before is not None
    assert pointer_before.release_digest != first.new_release_digest
    assert len(store.messages()) == 1

    recovered = store.recover()
    pointer_after = load_active_release_pointer(tmp_path)
    messages = store.messages()

    assert recovered is not None
    assert pointer_after is not None
    assert len(messages) == 2
    assert pointer_after.release_digest == messages[-1].new_release_digest
    assert messages[-1].prior_active_release_digest == first.new_release_digest
    assert messages[-1].new_release_digest != first.new_release_digest
    assert recovered.last_message_id == messages[-1].message_id


def test_rejects_concurrent_writers_traversal_secrets_and_stale_parent(tmp_path: Path) -> None:
    store = HarnessFactoryTransactionStore(tmp_path)
    request, search_digest, selection_evidence = _release_request("reject")

    with pytest.raises(ValueError, match="non-traversing"):
        store.create_initial_chat(
            request=request,
            user_prompt_text="Build a safe harness release.",
            chat_id="../escape",
            search_result_digest=search_digest,
            selection_evidence_digests=selection_evidence,
        )

    for prompt, match in (
        ("Please use api_key=abc in the prompt.", "non-public"),
        ("Bearer abcdefghijklmnopqrstuvwxyz012345", "credential"),
    ):
        with pytest.raises(HarnessFactoryValidationError, match=match):
            store.create_initial_chat(
                request=request,
                user_prompt_text=prompt,
                chat_id="chat.reject",
                search_result_digest=search_digest,
                selection_evidence_digests=selection_evidence,
            )

    lock_path = tmp_path / HARNESS_FACTORY_CHAT_DIR_NAME / ".transaction.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("writer", encoding="utf-8")
    with pytest.raises(HarnessFactoryConcurrencyError):
        store.create_initial_chat(
            request=request,
            user_prompt_text="Build while another writer holds the lock.",
            chat_id="chat.reject",
            search_result_digest=search_digest,
            selection_evidence_digests=selection_evidence,
        )
    lock_path.unlink()

    first = store.create_initial_chat(
        request=request,
        user_prompt_text="Build the accepted initial harness release.",
        chat_id="chat.reject",
        search_result_digest=search_digest,
        selection_evidence_digests=selection_evidence,
    )
    follow_request, follow_search, follow_selection = _release_request("reject-followup")
    with pytest.raises(HarnessFactoryStaleHeadError, match="parent message is stale"):
        store.apply_followup(
            request=follow_request,
            user_prompt_text="This follow-up names the wrong parent.",
            expected_parent_message_id="fmsg.not-the-parent",
            search_result_digest=follow_search,
            selection_evidence_digests=follow_selection,
        )
    assert load_active_release_pointer(tmp_path).release_digest == first.new_release_digest
    assert [message.message_id for message in store.messages()] == [first.message_id]


def test_selection_evidence_mismatch_fails_before_transaction_visibility(tmp_path: Path) -> None:
    store = HarnessFactoryTransactionStore(tmp_path)
    request, search_digest, _selection_evidence = _release_request("selection-mismatch")

    with pytest.raises(HarnessFactoryValidationError, match="selection evidence"):
        store.create_initial_chat(
            request=request,
            user_prompt_text="Build with crossed selection evidence.",
            chat_id="chat.selection",
            search_result_digest=search_digest,
            selection_evidence_digests=(_digest("different-selection"),),
        )

    assert load_active_release_pointer(tmp_path) is None
    assert not (tmp_path / HARNESS_FACTORY_CHAT_DIR_NAME / "chat.json").exists()
    assert store.messages() == []
