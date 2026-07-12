from __future__ import annotations

import json

import pytest

from agintor.evaluation.gate0 import (
    GATE0_ARMS,
    GATE0_PANEL_ITEM_COUNT,
    GATE0_REPLICATES_PER_ITEM,
    GATE0_TEMPLATE_IDS,
    Gate0AnalysisError,
    Gate0LiveExecutionBlocked,
    Gate0Observation,
    analyze_gate0_observations,
    build_gate0_dry_run_manifest,
    build_gate0_panel,
    build_gate0_provider_identity,
    require_gate0_live_authorization,
    validate_gate0_dry_run_conformance,
    write_gate0_preregistration,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
    harness_deployment_profile_digest,
)
from agintor.runtime.kernel.composite_provider import CredentialReference


def _provider():
    return build_gate0_provider_identity(
        deployment_profile=_live_profile(),
    )


def _manifest():
    return build_gate0_dry_run_manifest(
        provider_identity=_provider(),
        evidence_destination="gate0/preregistration.json",
    )


def _live_profile(
    *,
    model: str = "fixed-gate0-model",
    base_url: str = "https://api.openai.com/v1",
) -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="provider.fixed.gate0",
        provider="openai",
        model=model,
        endpoint=HarnessProviderEndpoint(
            base_url=base_url,
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


def _live_manifest(profile: HarnessDeploymentProfile):
    return build_gate0_dry_run_manifest(
        provider_identity=build_gate0_provider_identity(
            deployment_profile=profile,
        ),
        evidence_destination="controlled/gate0-live",
    )


def _credential_reference(
    profile: HarnessDeploymentProfile,
    *,
    api_key_env: str = "OPENAI_API_KEY",
) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=api_key_env,
    )


def _threshold_fixture_observations(manifest):
    observations = []
    for sample in manifest.arms:
        if sample.arm == "intact_exchange":
            correct = sample.replicate_index in {0, 1, 2}
        elif sample.arm == "matched_neutral_artifact":
            correct = sample.replicate_index == 0
        elif sample.arm == "full_information":
            correct = True
        else:
            correct = False
        observations.append(
            Gate0Observation(
                observation_id=f"fixture:{sample.sample_id}",
                manifest_digest=manifest.manifest_digest,
                item_id=sample.item_id,
                template_id=sample.template_id,
                replicate_index=sample.replicate_index,
                arm=sample.arm,
                pair_key=sample.pair_key,
                pair_key_digest=sample.pair_key_digest,
                provider_config_digest=manifest.provider_identity.provider_config_digest,
                source_kind="deterministic_fixture",
                hard_invalid=False,
                correct_answer=correct,
            )
        )
    return tuple(observations)


def test_frozen_panel_and_dry_run_manifest_are_locked_and_not_live():
    panel = build_gate0_panel()
    assert panel.item_count == GATE0_PANEL_ITEM_COUNT == 32
    assert panel.replicates_per_item == GATE0_REPLICATES_PER_ITEM == 4
    assert tuple(panel.templates) == GATE0_TEMPLATE_IDS
    assert len({item.item_id for item in panel.items}) == 32
    assert {item.template_id for item in panel.items} == set(GATE0_TEMPLATE_IDS)
    assert all(not item.private_a_alone_sufficient for item in panel.items)
    assert all(not item.private_b_alone_sufficient for item in panel.items)

    manifest = _manifest()
    rebuilt = _manifest()

    assert manifest.live_status == "not_run"
    assert manifest.manifest_digest == rebuilt.manifest_digest
    assert manifest.provider_call_schedule == rebuilt.provider_call_schedule
    assert len(manifest.arms) == 32 * 4 * len(GATE0_ARMS)
    assert manifest.total_provider_calls == 32 * 4 * len(GATE0_ARMS) * 2
    assert set(sample.arm for sample in manifest.arms) == set(GATE0_ARMS)
    assert all(not call.request_sent for sample in manifest.arms for call in sample.calls)
    assert len(set(manifest.provider_call_schedule)) == manifest.total_provider_calls
    assert tuple(sorted(manifest.provider_call_schedule)) != manifest.provider_call_schedule

    for item in manifest.panel.items:
        for sample in manifest.arms:
            if sample.item_id != item.item_id:
                continue
            for call in sample.calls:
                assert item.expected_answer not in json.dumps(call.request_payload, sort_keys=True)


def test_conformance_validates_digest_delivery_budget_and_atomic_preregistration(tmp_path):
    manifest = _manifest()

    report = validate_gate0_dry_run_conformance(manifest)
    assert report.live_status == "not_run"
    assert report.passed
    assert {check.name for check in report.checks} >= {
        "digest_semantics",
        "delivery_semantics",
        "budget_semantics",
        "intact_neutral_matching",
    }

    preregistration_path = tmp_path / "gate0_preregistration.json"
    written = write_gate0_preregistration(preregistration_path, manifest)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["live_status"] == "not_run"
    assert payload["manifest"]["manifest_digest"] == manifest.manifest_digest
    assert payload["conformance"]["passed"] is True
    assert "preregistration_digest" in payload
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(FileExistsError):
        write_gate0_preregistration(preregistration_path, manifest)

    tampered = manifest.model_copy(
        update={"total_provider_calls": manifest.total_provider_calls + 1}
    )
    tampered_report = validate_gate0_dry_run_conformance(tampered)
    assert not tampered_report.passed
    assert not next(check for check in tampered_report.checks if check.name == "budget_semantics").passed


def test_live_execution_guard_binds_frozen_profile_identity_and_env_name_only():
    profile = _live_profile()
    manifest = _live_manifest(profile)
    credential = _credential_reference(profile)

    with pytest.raises(Gate0LiveExecutionBlocked):
        require_gate0_live_authorization(
            manifest,
            deployment_profile=profile,
            live_authorized=False,
            credential_reference=credential,
        )
    with pytest.raises(Gate0LiveExecutionBlocked):
        require_gate0_live_authorization(
            manifest,
            deployment_profile=profile,
            live_authorized=True,
            credential_reference=None,
        )
    with pytest.raises(Gate0LiveExecutionBlocked):
        require_gate0_live_authorization(
            manifest,
            deployment_profile=profile,
            live_authorized=True,
            credential_reference="env:OPENAI_API_KEY",  # type: ignore[arg-type]
        )
    with pytest.raises(Gate0LiveExecutionBlocked, match="provider identity crossed"):
        require_gate0_live_authorization(
            manifest,
            deployment_profile=_live_profile(
                base_url="https://crossed.example/v1",
            ),
            live_authorized=True,
            credential_reference=credential,
        )
    with pytest.raises(Gate0LiveExecutionBlocked, match="endpoint policy"):
        require_gate0_live_authorization(
            manifest,
            deployment_profile=profile,
            live_authorized=True,
            credential_reference=_credential_reference(
                profile,
                api_key_env="OTHER_API_KEY",
            ),
        )

    authorization = require_gate0_live_authorization(
        manifest,
        deployment_profile=profile,
        live_authorized=True,
        credential_reference=credential,
    )
    assert authorization.live_authorized is True
    assert authorization.manifest_digest == manifest.manifest_digest
    assert authorization.provider_identity == manifest.provider_identity
    assert authorization.deployment_profile == profile
    assert authorization.profile_digest == harness_deployment_profile_digest(profile)
    assert authorization.credential_reference == credential
    assert authorization.authorization_digest


def test_analysis_uses_explicit_pair_keys_and_locked_thresholds_without_live_claims():
    manifest = _manifest()
    observations = _threshold_fixture_observations(manifest)

    report = analyze_gate0_observations(manifest=manifest, observations=observations)

    assert report.live_status == "not_run"
    assert report.observation_source_kind == "deterministic_fixture"
    assert report.numerical_gate_passed
    metrics = {metric.arm: metric for metric in report.arm_metrics}
    assert metrics["intact_exchange"].success_rate == 0.75
    assert metrics["matched_neutral_artifact"].success_rate == 0.25
    assert metrics["full_information"].success_rate == 1.0
    assert metrics["private_a_only"].success_rate == 0.0
    assert metrics["private_b_only"].success_rate == 0.0
    assert report.intact_minus_null_effect == 0.5
    assert report.clustered_one_sided_95_lower_bound > 0.15
    threshold_results = {result.name: result for result in report.threshold_results}
    assert threshold_results["max_hard_invalid_rate_per_arm"].threshold == 0.02
    assert threshold_results["hard_invalid_rate_spread"].threshold == 0.02
    assert threshold_results["full_information_success_rate"].threshold == 0.80
    assert threshold_results["private_a_only_success_rate"].threshold == 0.25
    assert threshold_results["private_b_only_success_rate"].threshold == 0.25
    assert threshold_results["intact_exchange_success_rate"].threshold == 0.70
    assert threshold_results["intact_minus_null_effect"].threshold == 0.30
    assert threshold_results["clustered_one_sided_95_lower_bound"].threshold == 0.15


def test_analysis_fails_closed_on_missing_duplicate_and_config_mismatched_pairs():
    manifest = _manifest()
    observations = _threshold_fixture_observations(manifest)

    with pytest.raises(Gate0AnalysisError, match="missing"):
        analyze_gate0_observations(manifest=manifest, observations=observations[:-1])

    with pytest.raises(Gate0AnalysisError, match="duplicate"):
        analyze_gate0_observations(
            manifest=manifest,
            observations=observations + (observations[0],),
        )

    crossed_config = observations[0].model_copy(
        update={"provider_config_digest": "0" * 64}
    )
    with pytest.raises(Gate0AnalysisError, match="provider configuration"):
        analyze_gate0_observations(
            manifest=manifest,
            observations=(crossed_config,) + observations[1:],
        )

    crossed_pair_key = observations[0].model_copy(
        update={"pair_key": observations[1].pair_key}
    )
    with pytest.raises(Gate0AnalysisError, match="PairKey"):
        analyze_gate0_observations(
            manifest=manifest,
            observations=(crossed_pair_key,) + observations[1:],
        )
