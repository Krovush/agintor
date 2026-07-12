from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.core.identity import evidence_digest
from agintor.evaluation.gate0 import (
    Gate0LiveExecutionBlocked,
    build_gate0_dry_run_manifest,
    build_gate0_provider_identity,
    require_gate0_live_authorization,
)
from agintor.evaluation.gate0_runner import (
    GATE0_LIVE_ENABLE_ENV,
    Gate0CallExecutionResult,
    Gate0CallUsage,
    replay_gate0_run,
    run_gate0_fixture,
    run_gate0_live,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)
from agintor.runtime.kernel.composite_provider import CredentialReference


def _manifest():
    profile = _live_profile(model="deterministic-fixture-model")
    provider = build_gate0_provider_identity(
        deployment_profile=profile,
    )
    return build_gate0_dry_run_manifest(
        provider_identity=provider,
        evidence_destination="controlled/gate0-runner",
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


def _live_manifest(
    profile: HarnessDeploymentProfile,
    *,
    destination: str = "controlled/gate0-live",
):
    provider = build_gate0_provider_identity(
        deployment_profile=profile,
    )
    return build_gate0_dry_run_manifest(
        provider_identity=provider,
        evidence_destination=destination,
    )


def _credential_reference(profile: HarnessDeploymentProfile) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=profile.endpoint.api_key_env,
        api_key_file_env=profile.endpoint.api_key_file_env,
    )


class ScriptedExecutor:
    def __init__(
        self,
        manifest,
        *,
        mode: str = "success",
        expected_deployment_profile: HarnessDeploymentProfile | None = None,
        expected_credential_reference: CredentialReference | None = None,
    ) -> None:
        self.manifest = manifest
        self.mode = mode
        self.expected_deployment_profile = expected_deployment_profile
        self.expected_credential_reference = expected_credential_reference
        self.calls = []
        self.call_by_id = {}
        self.result_by_call_id = {}
        self.expected_by_item = {
            item.item_id: item.expected_answer for item in manifest.panel.items
        }
        self.artifact_by_item_replicate = {}
        for arm in manifest.arms:
            if arm.arm == "intact_exchange":
                responder = arm.calls[1]
                self.artifact_by_item_replicate[(arm.item_id, arm.replicate_index)] = (
                    responder.request_payload["delivered_artifact"]["artifact_text"]
                )

    def execute(self, call_plan, *, deployment_profile, credential_reference):
        assert deployment_profile == self.expected_deployment_profile
        assert credential_reference == self.expected_credential_reference
        self.calls.append(call_plan.call_id)
        self.call_by_id[call_plan.call_id] = call_plan
        index = len(self.calls) - 1
        if self.mode == "missing" and index == 0:
            raise RuntimeError("scripted missing provider result")
        if call_plan.actor_id == "producer_a":
            artifact = self.artifact_by_item_replicate[
                (call_plan.item_id, call_plan.replicate_index)
            ]
            if self.mode == "artifact_mismatch" and index == 0:
                artifact = artifact + "-crossed"
            output = {"artifact_text": artifact}
        else:
            if call_plan.arm == "intact_exchange":
                correct = call_plan.replicate_index in {0, 1, 2}
            elif call_plan.arm == "matched_neutral_artifact":
                correct = call_plan.replicate_index == 0
            elif call_plan.arm == "full_information":
                correct = True
            else:
                correct = False
            output = {
                "answer": self.expected_by_item[call_plan.item_id] if correct else "WRONG"
            }
        response_id = f"fixture-response:{evidence_digest(call_plan.call_id)[:24]}"
        if self.mode == "duplicate" and index == 1:
            response_id = next(iter(self.result_by_call_id.values())).response_id
        status = "valid"
        failure_detail = None
        request_sent = True
        usage = Gate0CallUsage(
            priced_input_units=(
                call_plan.priced_input_units + 1
                if self.mode == "accounting" and index == 0
                else call_plan.priced_input_units
            ),
            output_units=len(json.dumps(output, sort_keys=True).encode("utf-8")),
            known_cost_usd=0.001,
        )
        latency_ms = 1
        if self.mode == "provider_failure" and index == 0:
            status = "provider_error"
            failure_detail = "scripted provider failure"
            request_sent = False
            response_id = None
            usage = None
            output = {}
        if self.mode == "deadline" and index == 0:
            status = "deadline_exceeded"
            failure_detail = "scripted deadline"
            request_sent = True
            response_id = None
            usage = Gate0CallUsage.unknown()
            output = {}
            latency_ms = 10
        result = Gate0CallExecutionResult(
            call_id=("crossed-call" if self.mode == "crossed" and index == 0 else call_plan.call_id),
            request_digest=call_plan.request_digest,
            context_digest=call_plan.context_digest,
            pair_key_digest=call_plan.pair_key_digest,
            provider_config_digest=self.manifest.provider_identity.provider_config_digest,
            request_sent=request_sent,
            response_id=response_id,
            output=output,
            usage=usage,
            latency_ms=latency_ms,
            invalid_status=status,
            failure_detail=failure_detail,
        )
        self.result_by_call_id[call_plan.call_id] = result
        return result


def test_gate0_usage_rejects_cache_subcategories_above_priced_input() -> None:
    with pytest.raises(ValidationError, match="cache-write input units"):
        Gate0CallUsage(
            priced_input_units=4,
            output_units=1,
            cached_input_units=3,
            cache_write_input_units=2,
            known_cost_usd=0.1,
        )


def test_fixture_runner_follows_exact_schedule_applies_only_paired_artifacts_and_replays(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    executor = ScriptedExecutor(manifest)
    evidence_root = tmp_path / "fixture-run"

    report = run_gate0_fixture(
        manifest=manifest,
        executor=executor,
        evidence_root=evidence_root,
    )

    assert report.status == "completed"
    assert report.provenance == "deterministic_fixture"
    assert report.live_status == "not_run"
    assert report.authorization_digest is None
    assert report.profile_digest is None
    assert report.completed_call_count == manifest.total_provider_calls == 1280
    assert report.completed_arm_count == len(manifest.arms) == 640
    assert report.observed_priced_input_units == manifest.total_priced_input_units
    assert report.analysis is not None
    assert report.numerical_gate_passed is True
    assert executor.calls == list(manifest.provider_call_schedule)
    assert len(list((evidence_root / "calls").glob("*.json"))) == 1280
    assert len(list((evidence_root / "arms").glob("*.json"))) == 640
    assert (evidence_root / "final_report.json").is_file()
    assert not (evidence_root / "partial_run.json").exists()
    assert replay_gate0_run(manifest=manifest, evidence_root=evidence_root) == report
    with pytest.raises(FileExistsError, match="resumeless"):
        run_gate0_fixture(
            manifest=manifest,
            executor=ScriptedExecutor(manifest),
            evidence_root=evidence_root,
        )

    producer_output_by_sample = {}
    for arm in manifest.arms:
        producer = arm.calls[0]
        producer_output_by_sample[arm.sample_id] = executor.result_by_call_id[
            producer.call_id
        ].output["artifact_text"]
    for arm in manifest.arms:
        responder = executor.call_by_id[arm.calls[1].call_id]
        payload = responder.request_payload
        if arm.arm in {"intact_exchange", "private_a_only"}:
            assert payload["delivered_artifact"]["artifact_text"] == producer_output_by_sample[
                arm.sample_id
            ]
        elif arm.arm == "matched_neutral_artifact":
            delivered = payload["delivered_artifact"]["artifact_text"]
            assert delivered != producer_output_by_sample[arm.sample_id]
            assert len(delivered) == len(producer_output_by_sample[arm.sample_id])
        else:
            assert "delivered_artifact" not in payload
        if arm.arm == "private_a_only":
            assert "private_evidence_b" not in payload
        if arm.arm == "private_b_only":
            assert "private_evidence_b" in payload
            assert "private_evidence_a" not in payload
        if arm.arm == "full_information":
            assert "private_evidence_a" in payload
            assert "private_evidence_b" in payload


@pytest.mark.parametrize(
    ("mode", "failure_code", "deadline_ms"),
    [
        ("missing", "executor_error", 120_000),
        ("duplicate", "duplicate_response_id", 120_000),
        ("crossed", "crossed_identity", 120_000),
        ("provider_failure", "provider_error", 120_000),
        ("accounting", "accounting_error", 120_000),
        ("deadline", "deadline_exceeded", 1),
        ("artifact_mismatch", "artifact_identity_mismatch", 120_000),
    ],
)
def test_fixture_runner_persists_fail_closed_partial_records_without_analysis(
    tmp_path: Path,
    mode: str,
    failure_code: str,
    deadline_ms: int,
) -> None:
    manifest = _manifest()
    executor = ScriptedExecutor(manifest, mode=mode)
    evidence_root = tmp_path / f"failed-{mode}"

    report = run_gate0_fixture(
        manifest=manifest,
        executor=executor,
        evidence_root=evidence_root,
        call_deadline_ms=deadline_ms,
    )

    assert report.status == "incomplete"
    assert report.failure is not None
    assert report.failure.failure_code == failure_code
    assert report.analysis is None
    assert report.numerical_gate_passed is False
    assert report.completed_call_count < manifest.total_provider_calls
    assert (evidence_root / "partial_run.json").is_file()
    assert (evidence_root / "final_report.json").is_file()
    assert replay_gate0_run(manifest=manifest, evidence_root=evidence_root) == report


class NonCooperativeHungExecutor(ScriptedExecutor):
    def __init__(self, manifest) -> None:
        super().__init__(manifest)
        self.started = threading.Event()
        self.unblock = threading.Event()
        self.late_result_returned = threading.Event()
        self.cancelled_call_ids: list[str] = []

    def execute(self, call_plan, *, deployment_profile, credential_reference):
        self.started.set()
        self.unblock.wait(timeout=2.0)
        result = super().execute(
            call_plan,
            deployment_profile=deployment_profile,
            credential_reference=credential_reference,
        )
        self.late_result_returned.set()
        return result

    def cancel(self, call_id: str) -> None:
        self.cancelled_call_ids.append(call_id)


def test_fixture_runner_supervises_hung_executor_records_unknown_timeout_and_ignores_late_result(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    executor = NonCooperativeHungExecutor(manifest)
    evidence_root = tmp_path / "hung-call"
    started = time.monotonic()

    report = run_gate0_fixture(
        manifest=manifest,
        executor=executor,
        evidence_root=evidence_root,
        call_deadline_ms=20,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert executor.started.is_set()
    assert executor.cancelled_call_ids == [manifest.provider_call_schedule[0]]
    assert report.status == "incomplete"
    assert report.failure is not None
    assert report.failure.failure_code == "deadline_exceeded"
    assert report.completed_call_count == 1
    assert report.completed_arm_count == 0
    assert report.unknown_usage_event_count == 1
    assert report.unknown_cost_event_count == 1
    assert report.observed_priced_input_units == 0
    assert report.total_known_cost_usd == 0.0
    assert report.total_estimated_cost_usd == 0.0

    call_files = list((evidence_root / "calls").glob("*.json"))
    assert len(call_files) == 1
    persisted = json.loads(call_files[0].read_text(encoding="utf-8"))
    assert persisted["result"]["invalid_status"] == "deadline_exceeded"
    assert persisted["result"]["usage"]["unknown_usage"] is True
    assert persisted["result"]["usage"]["unknown_cost"] is True
    assert "priced_input_units" not in persisted["result"]["usage"]
    assert "known_cost_usd" not in persisted["result"]["usage"]
    assert not (evidence_root / "arms").exists()

    executor.unblock.set()
    assert executor.late_result_returned.wait(timeout=0.5)
    assert json.loads(call_files[0].read_text(encoding="utf-8")) == persisted
    assert replay_gate0_run(manifest=manifest, evidence_root=evidence_root) == report


def test_live_runner_never_calls_executor_without_matching_explicit_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _live_profile()
    manifest = _live_manifest(profile)
    credential = _credential_reference(profile)
    executor = ScriptedExecutor(manifest)
    authorization = require_gate0_live_authorization(
        manifest,
        deployment_profile=profile,
        live_authorized=True,
        credential_reference=credential,
    )

    monkeypatch.delenv(GATE0_LIVE_ENABLE_ENV, raising=False)
    with pytest.raises(Gate0LiveExecutionBlocked, match=GATE0_LIVE_ENABLE_ENV):
        run_gate0_live(
            manifest=manifest,
            executor=executor,
            evidence_root=tmp_path / "live-disabled",
            authorization=authorization,
            live_execution_marker="live_gate0",
        )
    assert executor.calls == []

    monkeypatch.setenv(GATE0_LIVE_ENABLE_ENV, "1")
    crossed_manifest = _live_manifest(
        profile,
        destination="controlled/gate0-live-crossed",
    )
    crossed = require_gate0_live_authorization(
        crossed_manifest,
        deployment_profile=profile,
        live_authorized=True,
        credential_reference=credential,
    )
    with pytest.raises(Gate0LiveExecutionBlocked, match="crossed"):
        run_gate0_live(
            manifest=manifest,
            executor=executor,
            evidence_root=tmp_path / "live-crossed",
            authorization=crossed,
            live_execution_marker="live_gate0",
        )
    assert executor.calls == []

    with pytest.raises(Gate0LiveExecutionBlocked, match="provider identity crossed"):
        require_gate0_live_authorization(
            manifest,
            deployment_profile=_live_profile(
                base_url="https://crossed.example/v1",
            ),
            live_authorized=True,
            credential_reference=credential,
        )
    assert executor.calls == []

    with pytest.raises(Gate0LiveExecutionBlocked, match="marker"):
        run_gate0_live(
            manifest=manifest,
            executor=executor,
            evidence_root=tmp_path / "live-marker",
            authorization=authorization,
            live_execution_marker="wrong",  # type: ignore[arg-type]
        )
    assert executor.calls == []

    live_executor = ScriptedExecutor(
        manifest,
        mode="provider_failure",
        expected_deployment_profile=profile,
        expected_credential_reference=credential,
    )
    report = run_gate0_live(
        manifest=manifest,
        executor=live_executor,
        evidence_root=tmp_path / "live-fake-provider-failure",
        authorization=authorization,
        live_execution_marker="live_gate0",
    )
    assert report.provenance == "authorized_live"
    assert report.live_status == "executed"
    assert report.authorization_digest == authorization.authorization_digest
    assert report.profile_digest == authorization.profile_digest
    assert report.status == "incomplete"
    assert live_executor.calls == [manifest.provider_call_schedule[0]]
    assert getattr(run_gate0_live, "live_gate0_only") is True
