from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import ResearchEpochManifest, TaskEnvelope
from ..contracts.feasibility import (
    BaselineHeadroomAssessment,
    D0LiveBaselineProof,
    DevelopmentTaskFeasibilityManifest,
    FEASIBILITY_SCHEMA_VERSION,
    FeasibilityControlResult,
    PairedSearchBudgetProjection,
    ProviderBaselineDryRun,
    baseline_headroom_assessment_digest,
    d0_evaluation_contract_authority_digest,
)
from ..contracts.outcomes import OutcomeReceipt, PairKey, pair_key_digest
from ..contracts.run_evidence import assert_no_resolved_credentials
from ..core.identity import evidence_digest
from ..runtime.harness_profile import (
    HarnessDeploymentProfile,
    harness_deployment_profile_digest,
)
from ..runtime.kernel.composite_provider import CredentialReference
from ..search.promotion import assert_authoritative_outcome_receipt
from .contracts import (
    EvaluationContract,
    assert_evaluation_contract_bound,
    evaluation_canary_digests,
)
from .runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    RepoPatchExecutionBackend,
)
from .runners.repo_patch_runner import (
    RepoPatchEvaluatorRunner,
    RepoPatchRunnerResult,
    RepoPatchFixture,
)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class DevelopmentTaskFeasibilityRunner:
    """Evaluator-only D0 audit. It never invokes a model provider."""

    def __init__(self, command_backend: RepoPatchExecutionBackend) -> None:
        if not command_backend.is_isolated:
            raise ValueError("D0 requires an explicit isolated evaluator command backend")
        if command_backend.backend_id != IsolatedRepoPatchCommandBackend.backend_id:
            raise ValueError("D0 backend is not the V1 isolated repo-patch adapter")
        self.command_backend = command_backend

    def run(
        self,
        *,
        epoch: ResearchEpochManifest,
        task: TaskEnvelope,
        evaluation_contract: EvaluationContract,
        known_good_patch: Any,
        empty_patch: Any,
        plausible_wrong_patches: Sequence[Any],
        baseline_outcome_receipts: Sequence[OutcomeReceipt] = (),
        baseline_protocol_digest: str | None = None,
    ) -> DevelopmentTaskFeasibilityManifest:
        if task.data_state != "development":
            raise ValueError("D0 may inspect only tasks permanently assigned to development")
        if not plausible_wrong_patches:
            raise ValueError("D0 requires at least one plausible-wrong patch control")
        assert_evaluation_contract_bound(evaluation_contract, epoch=epoch, task=task)
        canary_values = tuple(canary.value for canary in evaluation_contract.canaries)
        canary_digests = evaluation_canary_digests(evaluation_contract)
        inspected_artifacts = [known_good_patch, empty_patch, *plausible_wrong_patches]
        if _contains_sealed_material(inspected_artifacts, canary_values, canary_digests):
            raise ValueError("D0 candidate controls contain evaluator-sealed canary material")

        fixture = RepoPatchFixture.from_evaluation_contract(
            evaluation_contract,
            public_test_commands=task.public_reproduction,
            timeout_s=max(step.timeout_ms for step in task.public_reproduction) / 1000.0,
        )
        evaluator = RepoPatchEvaluatorRunner(self.command_backend)
        known_first = evaluator.run(candidate_artifact=known_good_patch, fixture=fixture)
        known_second = evaluator.run(candidate_artifact=known_good_patch, fixture=fixture)
        empty_result = evaluator.run(candidate_artifact=empty_patch, fixture=fixture)
        wrong_results = [
            evaluator.run(candidate_artifact=artifact, fixture=fixture)
            for artifact in plausible_wrong_patches
        ]
        tamper_artifact = {
            "files": [
                {
                    "path": evaluation_contract.protected_paths[0],
                    "updated_content": "D0_PROTECTED_TAMPER_CONTROL = True\n",
                }
            ]
        }
        tamper_result = evaluator.run(candidate_artifact=tamper_artifact, fixture=fixture)

        controls = [
            _control_result(
                control_id="known_good",
                kind="known_good",
                artifact=known_good_patch,
                result=known_first,
                expected=True,
                replay=known_second,
            ),
            _control_result(
                control_id="empty",
                kind="empty",
                artifact=empty_patch,
                result=empty_result,
                expected=False,
            ),
            *[
                _control_result(
                    control_id=f"plausible_wrong.{index}",
                    kind="plausible_wrong",
                    artifact=artifact,
                    result=result,
                    expected=False,
                )
                for index, (artifact, result) in enumerate(
                    zip(plausible_wrong_patches, wrong_results, strict=True)
                )
            ],
            _control_result(
                control_id="protected_tamper",
                kind="protected_tamper",
                artifact=tamper_artifact,
                result=tamper_result,
                expected=False,
            ),
        ]
        all_results = [known_first, known_second, empty_result, *wrong_results, tamper_result]
        protected_integrity = bool(
            tamper_result.tampered_tests
            and not tamper_result.complete_repair
            and all(result.source_snapshot_unchanged for result in all_results)
        )
        identity_integrity = all(
            result.fixture_identity_matched
            and result.scratch_snapshot_matched
            and result.evaluation_contract_digest == evaluation_contract.evaluation_contract_digest
            and result.execution_backend_id == self.command_backend.backend_id
            for result in all_results
        )
        leakage_integrity = not _contains_sealed_material(
            [result.model_dump(mode="json") for result in all_results],
            canary_values,
            canary_digests,
        )
        clean_replay = controls[0].reproducible is True
        offline_passed = bool(
            all(control.passed for control in controls)
            and protected_integrity
            and identity_integrity
            and leakage_integrity
            and clean_replay
        )
        baseline = _assess_baseline_headroom(
            receipts=baseline_outcome_receipts,
            epoch=epoch,
            task=task,
            contract=evaluation_contract,
            expected_environment_digest=known_first.environment_digest,
        )
        inferred_protocol = baseline.protocol_digest or _optional_digest(
            baseline_protocol_digest,
            "baseline_protocol_digest",
        )
        if baseline.protocol_digest and baseline_protocol_digest:
            if baseline.protocol_digest != _optional_digest(baseline_protocol_digest, "baseline_protocol_digest"):
                raise ValueError("baseline protocol identity disagrees with supplied OutcomeReceipts")
        pair_keys = _baseline_pair_keys(
            receipts=baseline_outcome_receipts,
            epoch=epoch,
            task=task,
            environment_digest=known_first.environment_digest,
        )
        projection = _paired_search_projection(epoch, task)
        dry_run = ProviderBaselineDryRun(
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            baseline_protocol_digest=inferred_protocol,
            pair_keys=pair_keys,
            planned_provider_calls=len(pair_keys),
            projected_max_known_cost_usd=len(pair_keys) * task.ceilings.max_known_cost_usd,
            projected_max_estimated_cost_usd=len(pair_keys) * task.ceilings.max_estimated_cost_usd,
        )

        reasons: list[str] = []
        if not offline_passed:
            reasons.append("offline_controls_failed")
        if baseline.status == "not_measured":
            reasons.append("real_provider_baseline_not_run")
        elif baseline.status == "saturated":
            reasons.append("baseline_saturated_no_headroom")
        elif baseline.status == "uniform_failure":
            reasons.append("baseline_uniform_failure")
        if inferred_protocol is None:
            reasons.append("strong_baseline_protocol_not_pinned")
        if not projection.fits_frozen_epoch_budget:
            reasons.append("projected_paired_search_exceeds_frozen_budget")
        if not offline_passed or baseline.status in {"saturated", "uniform_failure"} or not projection.fits_frozen_epoch_budget:
            status = "fail"
        elif baseline.status == "not_measured":
            status = "pending_real_provider_baseline"
        else:
            status = "pass"
        search_authorized = status == "pass"
        backend_digest = known_first.execution_backend_digest
        manifest_id = "feasibility." + evidence_digest(
            {
                "epoch": epoch.epoch_manifest_digest,
                "task": task.task_manifest_digest,
                "evaluation_contract": evaluation_contract.evaluation_contract_digest,
                "controls": [control.outcome_fingerprint for control in controls],
                "baseline": list(baseline.receipt_digests),
            }
        )[:24]
        manifest = DevelopmentTaskFeasibilityManifest(
            manifest_id=manifest_id,
            epoch_id=epoch.epoch_id,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            task_manifest_id=task.task_manifest_id,
            task_manifest_digest=task.task_manifest_digest,
            evaluation_contract_id=evaluation_contract.evaluation_contract_id,
            evaluation_contract_digest=evaluation_contract.evaluation_contract_digest,
            execution_backend_id=self.command_backend.backend_id,
            execution_backend_digest=backend_digest,
            controls=tuple(controls),
            clean_replay_reproducible=clean_replay,
            protected_path_integrity=protected_integrity,
            leakage_integrity=leakage_integrity,
            identity_integrity=identity_integrity,
            offline_controls_passed=offline_passed,
            baseline_headroom=baseline,
            paired_search_projection=projection,
            provider_baseline_dry_run=dry_run,
            status=status,
            search_authorized=search_authorized,
            reason_codes=tuple(sorted(set(reasons))),
        )
        if _contains_sealed_material(manifest.model_dump(mode="json"), canary_values, canary_digests):
            raise ValueError("D0 dry-run manifest leaked evaluator-sealed material")
        return manifest


def _control_result(
    *,
    control_id: str,
    kind: Literal["known_good", "empty", "plausible_wrong", "protected_tamper"],
    artifact: Any,
    result: RepoPatchRunnerResult,
    expected: bool,
    replay: RepoPatchRunnerResult | None = None,
) -> FeasibilityControlResult:
    fingerprint = _outcome_fingerprint(result)
    replay_fingerprint = _outcome_fingerprint(replay) if replay is not None else None
    reproducible = fingerprint == replay_fingerprint if replay is not None else None
    passed = bool(
        result.complete_repair == expected
        and result.source_snapshot_unchanged
        and result.scratch_snapshot_matched
        and result.fixture_identity_matched
        and result.clean_copy_snapshot_unchanged
        and (reproducible is not False)
        and (kind != "protected_tamper" or result.tampered_tests)
    )
    return FeasibilityControlResult(
        control_id=control_id,
        control_kind=kind,
        artifact_digest=evidence_digest({"kind": "d0-control-artifact", "artifact": _json_safe(artifact)}),
        expected_complete_repair=expected,
        observed_complete_repair=result.complete_repair,
        evaluator_status=result.status,
        outcome_fingerprint=fingerprint,
        replay_fingerprint=replay_fingerprint,
        reproducible=reproducible,
        source_snapshot_unchanged=result.source_snapshot_unchanged,
        scratch_snapshot_matched=result.scratch_snapshot_matched,
        fixture_identity_matched=result.fixture_identity_matched,
        protected_tamper_detected=result.tampered_tests,
        passed=passed,
    )


def _outcome_fingerprint(result: RepoPatchRunnerResult | None) -> str | None:
    if result is None:
        return None
    commands = [
        *([result.patch_apply] if result.patch_apply is not None else []),
        *result.public_command_results,
        *result.hidden_command_results,
    ]
    return evidence_digest(
        {
            "kind": "d0-evaluator-outcome",
            "status": result.status,
            "complete_repair": result.complete_repair,
            "applied": result.applied,
            "public": result.public_tests_passed,
            "hidden": result.hidden_tests_passed,
            "tampered": result.tampered_tests,
            "tampered_paths": result.tampered_paths,
            "source": result.repo_snapshot_digest,
            "fixture": result.fixture_digest,
            "environment": result.environment_digest,
            "patch": result.patch_digest,
            "patched_clean": result.patched_clean_digest,
            "clean_copy_snapshot_unchanged": result.clean_copy_snapshot_unchanged,
            "workspace_drift": [
                item.model_dump(mode="json") for item in result.workspace_drift_evidence
            ],
            "commands": [
                {
                    "name": command.name,
                    "command_digest": command.command_digest,
                    "status": command.terminal_status,
                    "exit_code": command.exit_code,
                    "expected_exit_codes": command.expected_exit_codes,
                    "stdout_digest": command.stdout_digest,
                    "stderr_digest": command.stderr_digest,
                }
                for command in commands
            ],
        }
    )


def _assess_baseline_headroom(
    *,
    receipts: Sequence[OutcomeReceipt],
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    contract: EvaluationContract,
    expected_environment_digest: str,
) -> BaselineHeadroomAssessment:
    if not receipts:
        return BaselineHeadroomAssessment(
            status="not_measured",
            receipt_count=0,
            complete_repairs=0,
            failures=0,
        )
    expected_count = epoch.search_envelope.sampling_replicates
    if len(receipts) != expected_count:
        raise ValueError("baseline OutcomeReceipts must exactly cover frozen sampling replicates")
    pair_digests: set[str] = set()
    protocols: set[str] = set()
    configurations: set[tuple[str, str, str, str]] = set()
    for receipt in receipts:
        assert_authoritative_outcome_receipt(receipt, epoch)
        if receipt.task_manifest_id != task.task_manifest_id or receipt.task_manifest_digest != task.task_manifest_digest:
            raise ValueError("baseline OutcomeReceipt crossed the development task")
        if (
            receipt.evaluation_contract_id != contract.evaluation_contract_id
            or receipt.evaluation_contract_digest != contract.evaluation_contract_digest
        ):
            raise ValueError("baseline OutcomeReceipt crossed the EvaluationContract")
        if receipt.evaluator_environment_digest != expected_environment_digest:
            raise ValueError("baseline OutcomeReceipt crossed the clean evaluator environment")
        digest = pair_key_digest(receipt.pair_key)
        if digest in pair_digests:
            raise ValueError("baseline OutcomeReceipts contain duplicate PairKeys")
        pair_digests.add(digest)
        protocols.add(receipt.protocol_digest)
        configurations.add(
            (
                receipt.compiler_digest,
                receipt.kernel_digest,
                receipt.tool_manifest_digest,
                receipt.pair_key.environment_id,
            )
        )
    if len(protocols) != 1 or len(configurations) != 1:
        raise ValueError("baseline OutcomeReceipts are not one equal-envelope protocol panel")
    expected_replicates = set(range(expected_count))
    actual_replicates = {receipt.pair_key.sampling_replicate for receipt in receipts}
    if actual_replicates != expected_replicates:
        raise ValueError("baseline OutcomeReceipts do not cover canonical sampling replicates")
    complete = sum(1 for receipt in receipts if receipt.complete_repair)
    failures = len(receipts) - complete
    status = "has_headroom" if complete and failures else "saturated" if complete else "uniform_failure"
    return BaselineHeadroomAssessment(
        status=status,
        receipt_count=len(receipts),
        complete_repairs=complete,
        failures=failures,
        protocol_digest=next(iter(protocols)),
        receipt_digests=tuple(sorted(receipt.receipt_digest for receipt in receipts)),
        mean_model_calls=fmean(receipt.cost.model_calls for receipt in receipts),
        mean_wall_time_ms=fmean(receipt.cost.wall_time_ms for receipt in receipts),
        mean_known_cost_usd=fmean(receipt.cost.known_cost_usd for receipt in receipts),
        mean_estimated_cost_usd=fmean(
            receipt.cost.known_cost_usd + receipt.cost.estimated_cost_usd
            for receipt in receipts
        ),
    )


def _baseline_pair_keys(
    *,
    receipts: Sequence[OutcomeReceipt],
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    environment_digest: str,
) -> tuple[PairKey, ...]:
    if receipts:
        return tuple(sorted((receipt.pair_key for receipt in receipts), key=lambda key: key.sampling_replicate))
    environment_id = f"evaluator.{environment_digest[:24]}"
    return tuple(
        PairKey(
            task_manifest_id=task.task_manifest_id,
            environment_id=environment_id,
            sampling_replicate=index,
            provider_config_digest=epoch.deployment.provider_config_digest,
        )
        for index in range(epoch.search_envelope.sampling_replicates)
    )


def _paired_search_projection(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
) -> PairedSearchBudgetProjection:
    structural_capacity = epoch.search_envelope.max_steps * epoch.search_envelope.offspring_per_step
    candidate_budget = epoch.stop_rule.max_candidate_evaluations
    candidates = min(structural_capacity, candidate_budget)
    paired_runs = candidates * epoch.search_envelope.sampling_replicates * 2
    projected_calls = paired_runs * task.ceilings.max_model_calls
    projected_known = paired_runs * task.ceilings.max_known_cost_usd
    projected_estimated = paired_runs * task.ceilings.max_estimated_cost_usd
    frozen_runs = candidate_budget * epoch.search_envelope.sampling_replicates * 2
    frozen_calls = frozen_runs * epoch.per_run_ceilings.max_model_calls
    frozen_known = frozen_runs * epoch.per_run_ceilings.max_known_cost_usd
    frozen_estimated = frozen_runs * epoch.per_run_ceilings.max_estimated_cost_usd
    fits = bool(
        candidates <= candidate_budget
        and projected_calls <= frozen_calls
        and projected_known <= frozen_known
        and projected_estimated <= frozen_estimated
    )
    return PairedSearchBudgetProjection(
        structural_candidate_capacity=structural_capacity,
        frozen_candidate_budget=candidate_budget,
        projected_candidate_evaluations=candidates,
        sampling_replicates=epoch.search_envelope.sampling_replicates,
        projected_paired_outcome_runs=paired_runs,
        projected_max_model_calls=projected_calls,
        projected_max_known_cost_usd=projected_known,
        projected_max_estimated_cost_usd=projected_estimated,
        frozen_max_model_calls=frozen_calls,
        frozen_max_known_cost_usd=frozen_known,
        frozen_max_estimated_cost_usd=frozen_estimated,
        fits_frozen_epoch_budget=fits,
    )


def _contains_sealed_material(
    value: Any,
    canary_values: Sequence[str],
    canary_digests: Sequence[str],
) -> bool:
    serialized = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    return any(marker and marker in serialized for marker in [*canary_values, *canary_digests])


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _optional_digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


D0_LIVE_ENABLE_ENV = "AGINTOR_ENABLE_LIVE_D0"
D0_LIVE_AUTHORIZATION_SCHEMA_VERSION = "repo-repair-d0-live-authorization-v1"
D0_LIVE_CALL_REQUEST_SCHEMA_VERSION = "repo-repair-d0-live-call-request-v1"
D0_LIVE_CALL_RESULT_SCHEMA_VERSION = "repo-repair-d0-live-call-result-v1"
D0_LIVE_CALL_OBSERVATION_SCHEMA_VERSION = "repo-repair-d0-live-call-observation-v1"
D0_LIVE_RUN_SCHEMA_VERSION = "repo-repair-d0-live-baseline-run-v1"


class D0LiveExecutionBlocked(RuntimeError):
    """Raised before dispatch when explicit D0 live authority is absent or crossed."""


class D0LiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _required_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field_name} must be nonempty and NUL-free")
    return normalized


def _provider_dry_run_digest(value: ProviderBaselineDryRun) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-d0-provider-baseline-dry-run-v1",
            **value.model_dump(mode="python"),
        }
    )


def _credential_reference_digest(value: CredentialReference) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-credential-reference-v1",
            **value.model_dump(mode="python", exclude_none=True),
        }
    )


class D0LiveBaselineAuthorization(D0LiveModel):
    schema_version: Literal[D0_LIVE_AUTHORIZATION_SCHEMA_VERSION] = (
        D0_LIVE_AUTHORIZATION_SCHEMA_VERSION
    )
    authorization_id: str
    authorization_digest: str = ""
    live_authorized: Literal[True] = True
    live_execution_marker: Literal["live_d0"] = "live_d0"
    epoch_id: str
    epoch_manifest_digest: str
    task_manifest_id: str
    task_manifest_digest: str
    evaluation_contract_id: str
    evaluation_contract_digest: str
    provider_dry_run_digest: str
    deployment_profile: HarnessDeploymentProfile
    profile_digest: str
    baseline_protocol_digest: str
    pair_keys: tuple[PairKey, ...] = Field(min_length=1)
    credential_reference: CredentialReference
    credential_reference_digest: str
    call_deadline_ms: int = Field(gt=0, le=3_600_000)

    @field_validator(
        "authorization_id",
        "epoch_id",
        "task_manifest_id",
        "evaluation_contract_id",
    )
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _required_text(value, info.field_name)

    @field_validator(
        "authorization_digest",
        "epoch_manifest_digest",
        "task_manifest_digest",
        "evaluation_contract_digest",
        "provider_dry_run_digest",
        "profile_digest",
        "baseline_protocol_digest",
        "credential_reference_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "authorization_digest" and not value:
            return ""
        return _required_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_authorization(self) -> "D0LiveBaselineAuthorization":
        profile = self.deployment_profile
        if self.profile_digest != harness_deployment_profile_digest(profile):
            raise ValueError("D0 live authorization crossed its frozen deployment profile")
        if self.credential_reference.provider_name != profile.provider:
            raise ValueError("D0 credential reference crossed the frozen provider")
        if (
            self.credential_reference.api_key_env != profile.endpoint.api_key_env
            or self.credential_reference.api_key_file_env
            != profile.endpoint.api_key_file_env
        ):
            raise ValueError("D0 credential reference differs from the frozen endpoint policy")
        if self.credential_reference_digest != _credential_reference_digest(
            self.credential_reference
        ):
            raise ValueError("D0 credential-reference digest mismatch")
        pair_digests = tuple(pair_key_digest(pair) for pair in self.pair_keys)
        if len(pair_digests) != len(set(pair_digests)):
            raise ValueError("D0 live authorization contains duplicate PairKeys")
        if tuple(pair.sampling_replicate for pair in self.pair_keys) != tuple(
            range(len(self.pair_keys))
        ):
            raise ValueError("D0 live PairKeys must use canonical contiguous replicates")
        if len({pair.environment_id for pair in self.pair_keys}) != 1:
            raise ValueError("D0 live PairKeys must share one frozen environment")
        if any(
            pair.task_manifest_id != self.task_manifest_id
            or pair.provider_config_digest != profile.provider_config_digest
            for pair in self.pair_keys
        ):
            raise ValueError("D0 live PairKeys crossed task or provider authority")
        payload = self.model_dump(mode="python", exclude={"authorization_digest"})
        computed = evidence_digest(
            {"kind": D0_LIVE_AUTHORIZATION_SCHEMA_VERSION, **payload}
        )
        if self.authorization_digest and self.authorization_digest != computed:
            raise ValueError("D0 live authorization digest mismatch")
        if not self.authorization_digest:
            object.__setattr__(self, "authorization_digest", computed)
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


def require_d0_live_authorization(
    *,
    feasibility_manifest: DevelopmentTaskFeasibilityManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    evaluation_contract: EvaluationContract,
    deployment_profile: HarnessDeploymentProfile,
    baseline_protocol_digest: str,
    credential_reference: CredentialReference,
    call_deadline_ms: int = 120_000,
    live_authorized: bool,
) -> D0LiveBaselineAuthorization:
    if not live_authorized:
        raise D0LiveExecutionBlocked("D0 live baseline requires explicit live_authorized=True")
    profile = HarnessDeploymentProfile.model_validate(
        deployment_profile.model_dump(mode="python")
    )
    if profile.to_deployment_identity() != epoch.deployment:
        raise D0LiveExecutionBlocked("D0 deployment profile crossed the frozen epoch")
    if (
        feasibility_manifest.epoch_manifest_digest != epoch.epoch_manifest_digest
        or feasibility_manifest.task_manifest_digest != task.task_manifest_digest
        or feasibility_manifest.evaluation_contract_digest
        != evaluation_contract.evaluation_contract_digest
    ):
        raise D0LiveExecutionBlocked("D0 live authority crossed the feasibility manifest")
    dry_run = feasibility_manifest.provider_baseline_dry_run
    if dry_run.planned_provider_calls != len(dry_run.pair_keys):
        raise D0LiveExecutionBlocked("D0 provider dry-run call count crossed its PairKeys")
    protocol_digest = _required_digest(
        baseline_protocol_digest,
        "baseline_protocol_digest",
    )
    if dry_run.baseline_protocol_digest not in {None, protocol_digest}:
        raise D0LiveExecutionBlocked("D0 baseline protocol crossed the dry-run plan")
    credential = CredentialReference.model_validate(
        credential_reference.model_dump(mode="python")
    )
    return D0LiveBaselineAuthorization(
        authorization_id="d0-live-auth."
        + evidence_digest(
            {
                "manifest": feasibility_manifest.manifest_digest,
                "profile": harness_deployment_profile_digest(profile),
                "protocol": protocol_digest,
            }
        )[:24],
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id=evaluation_contract.evaluation_contract_id,
        evaluation_contract_digest=evaluation_contract.evaluation_contract_digest,
        provider_dry_run_digest=_provider_dry_run_digest(dry_run),
        deployment_profile=profile,
        profile_digest=harness_deployment_profile_digest(profile),
        baseline_protocol_digest=protocol_digest,
        pair_keys=dry_run.pair_keys,
        credential_reference=credential,
        credential_reference_digest=_credential_reference_digest(credential),
        call_deadline_ms=call_deadline_ms,
    )


class D0LiveBaselineCallRequest(D0LiveModel):
    schema_version: Literal[D0_LIVE_CALL_REQUEST_SCHEMA_VERSION] = (
        D0_LIVE_CALL_REQUEST_SCHEMA_VERSION
    )
    sequence_index: int = Field(ge=0)
    call_id: str
    request_digest: str = ""
    authorization_digest: str
    pair_key: PairKey
    baseline_protocol_digest: str
    profile_digest: str
    deployment_id: str
    provider: str
    model: str
    provider_config_digest: str
    call_deadline_ms: int = Field(gt=0)

    @field_validator("call_id", "deployment_id", "provider", "model")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _required_text(value, info.field_name)

    @field_validator(
        "request_digest",
        "authorization_digest",
        "baseline_protocol_digest",
        "profile_digest",
        "provider_config_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "request_digest" and not value:
            return ""
        return _required_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_request(self) -> "D0LiveBaselineCallRequest":
        payload = self.model_dump(mode="python", exclude={"request_digest"})
        computed = evidence_digest(
            {"kind": D0_LIVE_CALL_REQUEST_SCHEMA_VERSION, **payload}
        )
        if self.request_digest and self.request_digest != computed:
            raise ValueError("D0 live call request digest mismatch")
        if not self.request_digest:
            object.__setattr__(self, "request_digest", computed)
        return self


class D0LiveCallAccounting(D0LiveModel):
    execution_mode: Literal["live_provider"] = "live_provider"
    live_inference_status: Literal["completed", "failed"]
    request_sent: bool
    real_inference_requests_sent: int = Field(ge=0)
    usage_known: bool
    cost_known: bool
    model_calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    known_cost_usd: float | None = Field(default=None, ge=0.0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_accounting(self) -> "D0LiveCallAccounting":
        if self.usage_known and self.cache_write_tokens is None:
            object.__setattr__(self, "cache_write_tokens", 0)
        usage = (
            self.model_calls,
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.cache_write_tokens,
        )
        costs = (self.known_cost_usd, self.estimated_cost_usd)
        if self.request_sent != (self.real_inference_requests_sent > 0):
            raise ValueError("D0 request-sent and real-request counts disagree")
        if self.live_inference_status == "completed" and not self.request_sent:
            raise ValueError("completed D0 live inference requires a sent request")
        if self.usage_known != all(value is not None for value in usage):
            raise ValueError("D0 usage-known flag differs from numeric usage evidence")
        if (
            self.usage_known
            and self.input_tokens is not None
            and self.cached_tokens is not None
            and self.cache_write_tokens is not None
            and self.cached_tokens + self.cache_write_tokens > self.input_tokens
        ):
            raise ValueError("D0 cached and cache-write tokens exceed input tokens")
        if self.cost_known != all(value is not None for value in costs):
            raise ValueError("D0 cost-known flag differs from numeric cost evidence")
        return self

    @classmethod
    def from_receipt(cls, receipt: OutcomeReceipt) -> "D0LiveCallAccounting":
        cost = receipt.cost
        return cls(
            live_inference_status=receipt.live_inference_status,
            request_sent=receipt.real_inference_requests_sent > 0,
            real_inference_requests_sent=receipt.real_inference_requests_sent,
            usage_known=True,
            cost_known=not cost.unknown_dollars,
            model_calls=cost.model_calls,
            input_tokens=cost.input_tokens,
            output_tokens=cost.output_tokens,
            cached_tokens=cost.cached_tokens,
            cache_write_tokens=cost.cache_write_tokens,
            known_cost_usd=(None if cost.unknown_dollars else cost.known_cost_usd),
            estimated_cost_usd=(
                None if cost.unknown_dollars else cost.estimated_cost_usd
            ),
        )

    @classmethod
    def unknown_failure(cls) -> "D0LiveCallAccounting":
        return cls(
            live_inference_status="failed",
            request_sent=True,
            real_inference_requests_sent=1,
            usage_known=False,
            cost_known=False,
        )


class D0LiveBaselineCallResult(D0LiveModel):
    schema_version: Literal[D0_LIVE_CALL_RESULT_SCHEMA_VERSION] = (
        D0_LIVE_CALL_RESULT_SCHEMA_VERSION
    )
    call_id: str
    request_digest: str
    pair_key: PairKey
    status: Literal[
        "succeeded",
        "provider_error",
        "executor_error",
        "deadline_exceeded",
        "crossed_identity",
        "accounting_error",
    ]
    response_ids: tuple[str, ...] = ()
    accounting: D0LiveCallAccounting
    outcome_receipt: OutcomeReceipt | None = None
    failure_detail: str | None = None
    result_digest: str = ""

    @field_validator("call_id")
    @classmethod
    def validate_call_id(cls, value: str) -> str:
        return _required_text(value, "call_id")

    @field_validator("request_digest", "result_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "result_digest" and not value:
            return ""
        return _required_digest(value, info.field_name)

    @field_validator("response_ids")
    @classmethod
    def validate_response_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_required_text(item, "response_id") for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("D0 result response ids must be unique")
        return normalized

    @model_validator(mode="after")
    def bind_result(self) -> "D0LiveBaselineCallResult":
        if self.status == "succeeded":
            if self.outcome_receipt is None or self.failure_detail is not None:
                raise ValueError("successful D0 call requires only an OutcomeReceipt")
            if not self.response_ids:
                raise ValueError("successful D0 call requires provider response ids")
            if self.accounting.live_inference_status != "completed":
                raise ValueError("successful D0 call requires completed live provenance")
        elif self.outcome_receipt is not None or not self.failure_detail:
            raise ValueError("failed D0 call requires a detail and no OutcomeReceipt")
        payload = self.model_dump(mode="python", exclude={"result_digest"})
        computed = evidence_digest(
            {"kind": D0_LIVE_CALL_RESULT_SCHEMA_VERSION, **payload}
        )
        if self.result_digest and self.result_digest != computed:
            raise ValueError("D0 live call result digest mismatch")
        if not self.result_digest:
            object.__setattr__(self, "result_digest", computed)
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


@runtime_checkable
class D0LiveBaselineExecutor(Protocol):
    def execute(
        self,
        request: D0LiveBaselineCallRequest,
        *,
        credential_reference: CredentialReference,
    ) -> D0LiveBaselineCallResult: ...


class D0LiveBaselineCallObservation(D0LiveModel):
    schema_version: Literal[D0_LIVE_CALL_OBSERVATION_SCHEMA_VERSION] = (
        D0_LIVE_CALL_OBSERVATION_SCHEMA_VERSION
    )
    request: D0LiveBaselineCallRequest
    result: D0LiveBaselineCallResult
    observation_digest: str = ""

    @field_validator("observation_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return "" if not value else _required_digest(value, "observation_digest")

    @model_validator(mode="after")
    def bind_observation(self) -> "D0LiveBaselineCallObservation":
        if (
            self.result.call_id != self.request.call_id
            or self.result.request_digest != self.request.request_digest
            or self.result.pair_key != self.request.pair_key
        ):
            raise ValueError("D0 live call result crossed its exact request")
        payload = self.model_dump(mode="python", exclude={"observation_digest"})
        computed = evidence_digest(
            {"kind": D0_LIVE_CALL_OBSERVATION_SCHEMA_VERSION, **payload}
        )
        if self.observation_digest and self.observation_digest != computed:
            raise ValueError("D0 live call observation digest mismatch")
        if not self.observation_digest:
            object.__setattr__(self, "observation_digest", computed)
        return self


class D0LiveRunFailure(D0LiveModel):
    failure_code: Literal[
        "provider_error",
        "executor_error",
        "deadline_exceeded",
        "crossed_identity",
        "accounting_error",
        "persistence_error",
    ]
    sequence_index: int = Field(ge=0)
    call_id: str | None = None
    detail: str


class D0LiveBaselineReport(D0LiveModel):
    schema_version: Literal[D0_LIVE_RUN_SCHEMA_VERSION] = D0_LIVE_RUN_SCHEMA_VERSION
    execution_id: str
    execution_digest: str = ""
    authorization_digest: str
    provider_dry_run_digest: str
    evaluation_contract_authority_digest: str
    execution_mode: Literal["live_provider"] = "live_provider"
    live_inference_status: Literal["completed", "failed"]
    status: Literal["completed", "incomplete"]
    scheduled_pair_keys: tuple[PairKey, ...]
    call_observation_digests: tuple[str, ...]
    outcome_receipts: tuple[OutcomeReceipt, ...]
    completed_call_count: int = Field(ge=0)
    real_inference_requests_sent: int = Field(ge=0)
    total_model_calls: int = Field(ge=0)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_cached_tokens: int = Field(ge=0)
    total_cache_write_tokens: int = Field(default=0, ge=0)
    total_known_cost_usd: float = Field(ge=0.0)
    total_estimated_cost_usd: float = Field(ge=0.0)
    unknown_usage_event_count: int = Field(ge=0)
    unknown_cost_event_count: int = Field(ge=0)
    baseline_headroom: BaselineHeadroomAssessment | None = None
    failure: D0LiveRunFailure | None = None

    @field_validator(
        "execution_id",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _required_text(value, "execution_id")

    @field_validator(
        "execution_digest",
        "authorization_digest",
        "provider_dry_run_digest",
        "evaluation_contract_authority_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "execution_digest" and not value:
            return ""
        return _required_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_report(self) -> "D0LiveBaselineReport":
        if self.status == "completed":
            if (
                self.live_inference_status != "completed"
                or self.failure is not None
                or self.baseline_headroom is None
                or self.completed_call_count != len(self.scheduled_pair_keys)
                or len(self.outcome_receipts) != len(self.scheduled_pair_keys)
            ):
                raise ValueError("completed D0 live report must cover its exact schedule")
        elif self.failure is None or self.baseline_headroom is not None:
            raise ValueError("incomplete D0 live report must fail closed without headroom")
        payload = self.model_dump(mode="python", exclude={"execution_digest"})
        computed = evidence_digest({"kind": D0_LIVE_RUN_SCHEMA_VERSION, **payload})
        if self.execution_digest and self.execution_digest != computed:
            raise ValueError("D0 live report digest mismatch")
        if not self.execution_digest:
            object.__setattr__(self, "execution_digest", computed)
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


@dataclass(frozen=True, slots=True)
class _SupervisedD0Outcome:
    result: D0LiveBaselineCallResult | None = None
    error: Exception | None = None


class _D0EvidenceWriter:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.calls_root = self.root / "calls"
        if self.root.exists():
            raise FileExistsError("D0 live evidence root is resumeless and already exists")
        self.calls_root.mkdir(parents=True)

    @staticmethod
    def _write_once(path: Path, value: D0LiveModel) -> None:
        payload = (
            json.dumps(
                value.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def write_authorization(self, value: D0LiveBaselineAuthorization) -> None:
        self._write_once(self.root / "authorization.json", value)

    def write_call(self, value: D0LiveBaselineCallObservation) -> None:
        self._write_once(
            self.calls_root
            / f"{value.request.sequence_index:06d}-{pair_key_digest(value.request.pair_key)}.json",
            value,
        )

    def write_report(self, value: D0LiveBaselineReport) -> None:
        self._write_once(self.root / "final_report.json", value)

    def write_public_proof(self, value: D0LiveBaselineProof) -> None:
        self._write_once(self.root / "public_proof.json", value)


def _supervise_d0_call(
    *,
    executor: D0LiveBaselineExecutor,
    request: D0LiveBaselineCallRequest,
    credential_reference: CredentialReference,
    deadline_ms: int,
) -> D0LiveBaselineCallResult:
    results: queue.Queue[_SupervisedD0Outcome] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result = executor.execute(
                request,
                credential_reference=credential_reference,
            )
            outcome = _SupervisedD0Outcome(
                result=D0LiveBaselineCallResult.model_validate(
                    result.model_dump(mode="python")
                )
            )
        except Exception as exc:
            outcome = _SupervisedD0Outcome(error=exc)
        try:
            results.put_nowait(outcome)
        except queue.Full:
            pass

    threading.Thread(target=invoke, daemon=True).start()
    try:
        outcome = results.get(timeout=deadline_ms / 1000.0)
    except queue.Empty:
        cancel = getattr(executor, "cancel", None)
        if callable(cancel):
            try:
                cancel(request.call_id)
            except Exception:
                pass
        return D0LiveBaselineCallResult(
            call_id=request.call_id,
            request_digest=request.request_digest,
            pair_key=request.pair_key,
            status="deadline_exceeded",
            accounting=D0LiveCallAccounting.unknown_failure(),
            failure_detail="supervised D0 baseline call exceeded its frozen deadline",
        )
    if outcome.error is not None:
        return D0LiveBaselineCallResult(
            call_id=request.call_id,
            request_digest=request.request_digest,
            pair_key=request.pair_key,
            status="executor_error",
            accounting=D0LiveCallAccounting.unknown_failure(),
            failure_detail=f"{type(outcome.error).__name__}: executor failed without typed evidence",
        )
    if outcome.result is None:
        raise RuntimeError("supervised D0 executor returned no result or error")
    return outcome.result


def _validate_d0_live_result(
    *,
    result: D0LiveBaselineCallResult,
    request: D0LiveBaselineCallRequest,
    authorization: D0LiveBaselineAuthorization,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    contract: EvaluationContract,
) -> None:
    if (
        result.call_id != request.call_id
        or result.request_digest != request.request_digest
        or result.pair_key != request.pair_key
    ):
        raise ValueError("D0 executor result crossed its exact request")
    if result.status != "succeeded":
        return
    receipt = result.outcome_receipt
    if receipt is None:
        raise ValueError("successful D0 result omitted OutcomeReceipt")
    assert_authoritative_outcome_receipt(receipt, epoch)
    expected = {
        "task_manifest_id": task.task_manifest_id,
        "task_manifest_digest": task.task_manifest_digest,
        "evaluation_contract_id": contract.evaluation_contract_id,
        "evaluation_contract_digest": contract.evaluation_contract_digest,
        "profile_digest": authorization.profile_digest,
        "execution_mode": "live_provider",
        "live_inference_status": "completed",
        "protocol_digest": authorization.baseline_protocol_digest,
        "provider_config_digest": epoch.deployment.provider_config_digest,
        "decoding_policy_digest": epoch.deployment.decoding_policy_digest,
        "price_schedule_digest": epoch.deployment.price_schedule_digest,
        "command_container_policy_digest": (
            epoch.deployment.command_container_policy_digest
        ),
    }
    crossed = [
        name
        for name, value in expected.items()
        if getattr(receipt, name) != value
    ]
    if crossed or receipt.pair_key != request.pair_key:
        raise ValueError("D0 OutcomeReceipt crossed live authority: " + ", ".join(crossed))
    accounting = result.accounting
    cost = receipt.cost
    accounting_expected = {
        "real_inference_requests_sent": receipt.real_inference_requests_sent,
        "model_calls": cost.model_calls,
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
        "cached_tokens": cost.cached_tokens,
        "cache_write_tokens": cost.cache_write_tokens,
        "known_cost_usd": cost.known_cost_usd,
        "estimated_cost_usd": cost.estimated_cost_usd,
    }
    if (
        not accounting.usage_known
        or not accounting.cost_known
        or any(getattr(accounting, name) != value for name, value in accounting_expected.items())
        or cost.unknown_dollars
        or not cost.within_epoch_envelope
        or len(result.response_ids) != receipt.real_inference_requests_sent
    ):
        raise ValueError("D0 live accounting crossed authoritative OutcomeReceipt")


def run_d0_live_provider_baseline(
    *,
    feasibility_manifest: DevelopmentTaskFeasibilityManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    evaluation_contract: EvaluationContract,
    executor: D0LiveBaselineExecutor,
    authorization: D0LiveBaselineAuthorization,
    evidence_root: str | Path,
    live_execution_marker: Literal["live_d0"],
) -> D0LiveBaselineReport:
    """LIVE-ONLY D0 baseline runner. Provider behavior is injected explicitly."""

    if live_execution_marker != "live_d0":
        raise D0LiveExecutionBlocked("D0 live runner requires the explicit live_d0 marker")
    if os.environ.get(D0_LIVE_ENABLE_ENV, "").strip() != "1":
        raise D0LiveExecutionBlocked(
            f"D0 live runner requires {D0_LIVE_ENABLE_ENV}=1"
        )
    try:
        auth = D0LiveBaselineAuthorization.model_validate(
            authorization.model_dump(mode="python")
        )
    except Exception as exc:
        raise D0LiveExecutionBlocked("D0 live authorization failed strict validation") from exc
    if (
        auth.epoch_id != epoch.epoch_id
        or auth.epoch_manifest_digest != epoch.epoch_manifest_digest
        or auth.task_manifest_id != task.task_manifest_id
        or auth.task_manifest_digest != task.task_manifest_digest
        or auth.evaluation_contract_id != evaluation_contract.evaluation_contract_id
        or auth.evaluation_contract_digest
        != evaluation_contract.evaluation_contract_digest
        or auth.provider_dry_run_digest
        != _provider_dry_run_digest(feasibility_manifest.provider_baseline_dry_run)
        or auth.deployment_profile.to_deployment_identity() != epoch.deployment
        or auth.pair_keys != feasibility_manifest.provider_baseline_dry_run.pair_keys
        or feasibility_manifest.provider_baseline_dry_run.planned_provider_calls
        != len(auth.pair_keys)
    ):
        raise D0LiveExecutionBlocked("D0 live authorization crossed frozen feasibility authority")
    if feasibility_manifest.provider_baseline_dry_run.real_provider_baseline_status != "not_run":
        raise D0LiveExecutionBlocked("D0 live runner requires the exact not-run provider plan")
    writer = _D0EvidenceWriter(evidence_root)
    writer.write_authorization(auth)
    observations: list[D0LiveBaselineCallObservation] = []
    receipts: list[OutcomeReceipt] = []
    response_ids: set[str] = set()
    failure: D0LiveRunFailure | None = None
    for index, pair_key in enumerate(auth.pair_keys):
        request = D0LiveBaselineCallRequest(
            sequence_index=index,
            call_id=f"d0-baseline.{index:06d}.{pair_key_digest(pair_key)[:16]}",
            authorization_digest=auth.authorization_digest,
            pair_key=pair_key,
            baseline_protocol_digest=auth.baseline_protocol_digest,
            profile_digest=auth.profile_digest,
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            call_deadline_ms=auth.call_deadline_ms,
        )
        result = _supervise_d0_call(
            executor=executor,
            request=request,
            credential_reference=auth.credential_reference,
            deadline_ms=auth.call_deadline_ms,
        )
        try:
            _validate_d0_live_result(
                result=result,
                request=request,
                authorization=auth,
                epoch=epoch,
                task=task,
                contract=evaluation_contract,
            )
            duplicates = response_ids.intersection(result.response_ids)
            if duplicates:
                raise ValueError("D0 live baseline reused provider response ids")
            response_ids.update(result.response_ids)
        except Exception as exc:
            result = D0LiveBaselineCallResult(
                call_id=request.call_id,
                request_digest=request.request_digest,
                pair_key=request.pair_key,
                status=(
                    "accounting_error"
                    if "accounting" in str(exc).casefold()
                    else "crossed_identity"
                ),
                accounting=result.accounting,
                failure_detail=str(exc),
            )
        observation = D0LiveBaselineCallObservation(request=request, result=result)
        writer.write_call(observation)
        observations.append(observation)
        if result.status != "succeeded":
            failure = D0LiveRunFailure(
                failure_code=result.status,
                sequence_index=index,
                call_id=request.call_id,
                detail=result.failure_detail or "D0 live baseline failed closed",
            )
            break
        if result.outcome_receipt is None:
            raise RuntimeError("validated D0 success omitted OutcomeReceipt")
        receipts.append(result.outcome_receipt)

    accounting_rows = [observation.result.accounting for observation in observations]
    completed = failure is None and len(receipts) == len(auth.pair_keys)
    headroom = (
        _assess_baseline_headroom(
            receipts=receipts,
            epoch=epoch,
            task=task,
            contract=evaluation_contract,
            expected_environment_digest=receipts[0].evaluator_environment_digest,
        )
        if completed
        else None
    )
    report = D0LiveBaselineReport(
        execution_id="d0-live."
        + evidence_digest(
            {
                "authorization": auth.authorization_digest,
                "observations": [item.observation_digest for item in observations],
            }
        )[:24],
        authorization_digest=auth.authorization_digest,
        provider_dry_run_digest=auth.provider_dry_run_digest,
        evaluation_contract_authority_digest=(
            d0_evaluation_contract_authority_digest(
                evaluation_contract_id=auth.evaluation_contract_id,
                evaluation_contract_digest=auth.evaluation_contract_digest,
            )
        ),
        live_inference_status=("completed" if completed else "failed"),
        status=("completed" if completed else "incomplete"),
        scheduled_pair_keys=auth.pair_keys,
        call_observation_digests=tuple(
            observation.observation_digest for observation in observations
        ),
        outcome_receipts=tuple(receipts),
        completed_call_count=len(receipts),
        real_inference_requests_sent=sum(
            row.real_inference_requests_sent for row in accounting_rows
        ),
        total_model_calls=sum(row.model_calls or 0 for row in accounting_rows),
        total_input_tokens=sum(row.input_tokens or 0 for row in accounting_rows),
        total_output_tokens=sum(row.output_tokens or 0 for row in accounting_rows),
        total_cached_tokens=sum(row.cached_tokens or 0 for row in accounting_rows),
        total_cache_write_tokens=sum(
            row.cache_write_tokens or 0 for row in accounting_rows
        ),
        total_known_cost_usd=sum(row.known_cost_usd or 0.0 for row in accounting_rows),
        total_estimated_cost_usd=sum(
            row.estimated_cost_usd or 0.0 for row in accounting_rows
        ),
        unknown_usage_event_count=sum(not row.usage_known for row in accounting_rows),
        unknown_cost_event_count=sum(not row.cost_known for row in accounting_rows),
        baseline_headroom=headroom,
        failure=failure,
    )
    _reconcile_d0_report(report, tuple(observations), auth)
    public_proof = (
        d0_live_baseline_public_proof(
            report=report,
            authorization=auth,
        )
        if completed
        else None
    )
    writer.write_report(report)
    if public_proof is not None:
        writer.write_public_proof(public_proof)
    return report


run_d0_live_provider_baseline.live_d0_only = True  # type: ignore[attr-defined]


def _d0_json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"D0 evidence JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def _read_d0_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"D0 evidence file is missing or unsafe: {path.name}")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"D0 evidence file exceeds the byte limit: {path.name}")
    try:
        value = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_d0_json_object_no_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"D0 evidence file is not strict UTF-8 JSON: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"D0 evidence JSON root must be an object: {path.name}")
    return dict(value)


def _reconcile_d0_report(
    report: D0LiveBaselineReport,
    observations: tuple[D0LiveBaselineCallObservation, ...],
    authorization: D0LiveBaselineAuthorization,
) -> None:
    if (
        report.authorization_digest != authorization.authorization_digest
        or report.provider_dry_run_digest != authorization.provider_dry_run_digest
        or report.evaluation_contract_authority_digest
        != d0_evaluation_contract_authority_digest(
            evaluation_contract_id=authorization.evaluation_contract_id,
            evaluation_contract_digest=authorization.evaluation_contract_digest,
        )
        or report.scheduled_pair_keys != authorization.pair_keys
        or report.call_observation_digests
        != tuple(item.observation_digest for item in observations)
    ):
        raise ValueError("D0 report crossed authorization or call evidence")
    if tuple(item.request.sequence_index for item in observations) != tuple(
        range(len(observations))
    ):
        raise ValueError("D0 call evidence sequence is not contiguous")
    if tuple(item.request.pair_key for item in observations) != authorization.pair_keys[
        : len(observations)
    ]:
        raise ValueError("D0 call evidence crossed the scheduled PairKeys")
    if any(
        item.request.authorization_digest != authorization.authorization_digest
        for item in observations
    ):
        raise ValueError("D0 call evidence crossed live authorization")
    successful_receipts = tuple(
        item.result.outcome_receipt
        for item in observations
        if item.result.status == "succeeded"
    )
    if None in successful_receipts or report.outcome_receipts != successful_receipts:
        raise ValueError("D0 report OutcomeReceipts crossed call evidence")
    response_ids = tuple(
        response_id
        for item in observations
        for response_id in item.result.response_ids
    )
    if len(response_ids) != len(set(response_ids)):
        raise ValueError("D0 replay contains duplicate provider response ids")
    accounting = tuple(item.result.accounting for item in observations)
    expected = {
        "completed_call_count": len(successful_receipts),
        "real_inference_requests_sent": sum(
            row.real_inference_requests_sent for row in accounting
        ),
        "total_model_calls": sum(row.model_calls or 0 for row in accounting),
        "total_input_tokens": sum(row.input_tokens or 0 for row in accounting),
        "total_output_tokens": sum(row.output_tokens or 0 for row in accounting),
        "total_cached_tokens": sum(row.cached_tokens or 0 for row in accounting),
        "total_cache_write_tokens": sum(row.cache_write_tokens or 0 for row in accounting),
        "total_known_cost_usd": sum(row.known_cost_usd or 0.0 for row in accounting),
        "total_estimated_cost_usd": sum(
            row.estimated_cost_usd or 0.0 for row in accounting
        ),
        "unknown_usage_event_count": sum(not row.usage_known for row in accounting),
        "unknown_cost_event_count": sum(not row.cost_known for row in accounting),
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(report, field_name) != expected_value
    ]
    if crossed:
        raise ValueError("D0 report accounting crossed call evidence: " + ", ".join(crossed))


def d0_live_baseline_public_proof(
    *,
    report: D0LiveBaselineReport,
    authorization: D0LiveBaselineAuthorization,
) -> D0LiveBaselineProof:
    """Project evaluator-private D0 evidence into the factory-safe proof boundary."""

    full_report = D0LiveBaselineReport.model_validate(
        report.model_dump(mode="python")
    )
    auth = D0LiveBaselineAuthorization.model_validate(
        authorization.model_dump(mode="python")
    )
    contract_authority_digest = d0_evaluation_contract_authority_digest(
        evaluation_contract_id=auth.evaluation_contract_id,
        evaluation_contract_digest=auth.evaluation_contract_digest,
    )
    headroom = full_report.baseline_headroom
    report_receipts = full_report.outcome_receipts
    if (
        full_report.authorization_digest != auth.authorization_digest
        or full_report.provider_dry_run_digest != auth.provider_dry_run_digest
        or full_report.evaluation_contract_authority_digest
        != contract_authority_digest
        or full_report.scheduled_pair_keys != auth.pair_keys
        or full_report.execution_mode != "live_provider"
        or full_report.live_inference_status != "completed"
        or full_report.status != "completed"
        or headroom is None
        or headroom.status != "has_headroom"
        or headroom.protocol_digest != auth.baseline_protocol_digest
        or full_report.completed_call_count != len(auth.pair_keys)
        or len(report_receipts) != len(auth.pair_keys)
        or full_report.real_inference_requests_sent <= 0
        or full_report.total_model_calls
        != full_report.real_inference_requests_sent
        or full_report.unknown_usage_event_count != 0
        or full_report.unknown_cost_event_count != 0
    ):
        raise ValueError("D0 public proof requires one complete, healthy live baseline")
    receipts_by_pair_digest = {
        pair_key_digest(receipt.pair_key): receipt
        for receipt in report_receipts
    }
    scheduled_pair_digests = tuple(pair_key_digest(pair) for pair in auth.pair_keys)
    if (
        len(receipts_by_pair_digest) != len(report_receipts)
        or set(receipts_by_pair_digest) != set(scheduled_pair_digests)
    ):
        raise ValueError("D0 public proof receipts do not exactly cover scheduled PairKeys")
    receipts = tuple(
        receipts_by_pair_digest[pair_digest]
        for pair_digest in scheduled_pair_digests
    )
    receipt_digests = tuple(receipt.receipt_digest for receipt in receipts)
    if (
        headroom.receipt_digests != tuple(sorted(receipt_digests))
        or headroom.receipt_count != len(receipts)
    ):
        raise ValueError("D0 public proof receipts crossed headroom authority")
    profile = auth.deployment_profile
    first_receipt = receipts[0]
    evaluator_identity = (
        first_receipt.evaluator_id,
        first_receipt.evaluator_identity_digest,
        first_receipt.evaluation_policy_digest,
    )
    expected_receipt_fields = {
        "execution_mode": "live_provider",
        "live_inference_status": "completed",
        "epoch_id": auth.epoch_id,
        "epoch_manifest_digest": auth.epoch_manifest_digest,
        "task_manifest_id": auth.task_manifest_id,
        "task_manifest_digest": auth.task_manifest_digest,
        "evaluation_contract_id": auth.evaluation_contract_id,
        "evaluation_contract_digest": auth.evaluation_contract_digest,
        "profile_digest": auth.profile_digest,
        "protocol_digest": auth.baseline_protocol_digest,
        "provider_config_digest": profile.provider_config_digest,
        "decoding_policy_digest": profile.decoding_policy_digest,
        "price_schedule_digest": profile.price_schedule_digest,
        "command_container_policy_digest": profile.command_container_policy_digest,
    }
    for receipt in receipts:
        if (
            any(
                getattr(receipt, field_name) != expected
                for field_name, expected in expected_receipt_fields.items()
            )
            or (
                receipt.evaluator_id,
                receipt.evaluator_identity_digest,
                receipt.evaluation_policy_digest,
            )
            != evaluator_identity
            or receipt.real_inference_requests_sent <= 0
            or not receipt.health.passes_promotion_floor
            or bool(receipt.exclusions)
            or receipt.cost.unknown_dollars
            or not receipt.cost.within_epoch_envelope
        ):
            raise ValueError("D0 public proof receipt crossed evaluator or deployment authority")
    if (
        sum(receipt.real_inference_requests_sent for receipt in receipts)
        != full_report.real_inference_requests_sent
        or sum(receipt.cost.model_calls for receipt in receipts)
        != full_report.total_model_calls
        or sum(receipt.cost.known_cost_usd for receipt in receipts)
        != full_report.total_known_cost_usd
        or sum(receipt.cost.estimated_cost_usd for receipt in receipts)
        != full_report.total_estimated_cost_usd
    ):
        raise ValueError("D0 public proof aggregate provenance crossed its receipts")
    complete_repairs = sum(receipt.complete_repair for receipt in receipts)
    failures = len(receipts) - complete_repairs
    if (
        complete_repairs != headroom.complete_repairs
        or failures != headroom.failures
        or complete_repairs == 0
        or failures == 0
    ):
        raise ValueError("D0 public proof headroom is not a mixed authoritative baseline")
    proof = D0LiveBaselineProof(
        authorization_digest=auth.authorization_digest,
        report_digest=full_report.execution_digest,
        provider_dry_run_digest=auth.provider_dry_run_digest,
        epoch_id=auth.epoch_id,
        epoch_manifest_digest=auth.epoch_manifest_digest,
        task_manifest_id=auth.task_manifest_id,
        task_manifest_digest=auth.task_manifest_digest,
        deployment_id=profile.deployment_id,
        provider=profile.provider,
        model=profile.model,
        provider_config_digest=profile.provider_config_digest,
        decoding_policy_digest=profile.decoding_policy_digest,
        price_schedule_digest=profile.price_schedule_digest,
        command_container_policy_digest=profile.command_container_policy_digest,
        profile_digest=auth.profile_digest,
        evaluator_id=first_receipt.evaluator_id,
        evaluator_identity_digest=first_receipt.evaluator_identity_digest,
        evaluation_policy_digest=first_receipt.evaluation_policy_digest,
        evaluation_contract_authority_digest=contract_authority_digest,
        baseline_protocol_digest=auth.baseline_protocol_digest,
        pair_keys=auth.pair_keys,
        pair_key_digests=tuple(pair_key_digest(pair) for pair in auth.pair_keys),
        scheduled_pair_count=len(auth.pair_keys),
        completed_call_count=full_report.completed_call_count,
        receipt_digests=receipt_digests,
        receipt_count=len(receipts),
        complete_repairs=complete_repairs,
        failures=failures,
        baseline_headroom_digest=baseline_headroom_assessment_digest(headroom),
        real_inference_requests_sent=full_report.real_inference_requests_sent,
        total_model_calls=full_report.total_model_calls,
        total_known_cost_usd=full_report.total_known_cost_usd,
        total_estimated_cost_usd=full_report.total_estimated_cost_usd,
    )
    assert_no_resolved_credentials(proof.model_dump(mode="json"))
    return proof


def replay_d0_live_provider_baseline(
    *,
    feasibility_manifest: DevelopmentTaskFeasibilityManifest,
    authorization: D0LiveBaselineAuthorization,
    evidence_root: str | Path,
) -> D0LiveBaselineReport:
    root = Path(evidence_root).expanduser().resolve()
    auth = D0LiveBaselineAuthorization.model_validate(
        _read_d0_json(root / "authorization.json")
    )
    expected = D0LiveBaselineAuthorization.model_validate(
        authorization.model_dump(mode="python")
    )
    if (
        auth != expected
        or auth.provider_dry_run_digest
        != _provider_dry_run_digest(feasibility_manifest.provider_baseline_dry_run)
    ):
        raise ValueError("D0 replay crossed authorization or provider dry-run authority")
    report = D0LiveBaselineReport.model_validate(
        _read_d0_json(root / "final_report.json")
    )
    call_paths = sorted((root / "calls").glob("*.json"))
    observations = tuple(
        D0LiveBaselineCallObservation.model_validate(
            _read_d0_json(path)
        )
        for path in call_paths
    )
    _reconcile_d0_report(report, observations, auth)
    public_proof_path = root / "public_proof.json"
    if report.status == "completed":
        persisted_proof = D0LiveBaselineProof.model_validate(
            _read_d0_json(public_proof_path)
        )
        expected_proof = d0_live_baseline_public_proof(
            report=report,
            authorization=auth,
        )
        if persisted_proof != expected_proof:
            raise ValueError("D0 public proof crossed evaluator-private report evidence")
    elif public_proof_path.exists():
        raise ValueError("incomplete D0 evidence may not publish a live baseline proof")
    return report


__all__ = [
    "BaselineHeadroomAssessment",
    "D0_LIVE_ENABLE_ENV",
    "D0LiveBaselineAuthorization",
    "D0LiveBaselineCallObservation",
    "D0LiveBaselineCallRequest",
    "D0LiveBaselineCallResult",
    "D0LiveBaselineExecutor",
    "D0LiveBaselineProof",
    "D0LiveBaselineReport",
    "D0LiveCallAccounting",
    "D0LiveExecutionBlocked",
    "D0LiveRunFailure",
    "DevelopmentTaskFeasibilityManifest",
    "DevelopmentTaskFeasibilityRunner",
    "d0_live_baseline_public_proof",
    "FEASIBILITY_SCHEMA_VERSION",
    "FeasibilityControlResult",
    "PairedSearchBudgetProjection",
    "ProviderBaselineDryRun",
    "replay_d0_live_provider_baseline",
    "require_d0_live_authorization",
    "run_d0_live_provider_baseline",
]
