from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ...contracts.evidence import RuntimeEvidenceManifest
from ...evaluation.runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    TrustedLocalRepoPatchCommandBackend,
)
from ...evaluation.runners.repo_patch_runner import RepoPatchEvaluatorRunner, RepoPatchFixture
from ...isolation.commands import DockerCommandBackend, IsolatedCommandPolicy
from ..validator_registry import ValidatorFamily


_ARTIFACT_FLAG_KEYS = {
    "applied",
    "public_tests_passed",
    "hidden_tests_passed",
    "tampered_tests",
    "clean_copy_snapshot_unchanged",
    "evaluator_receipt",
    "repo_patch_result",
}


def _manifest(payload: dict[str, Any]) -> tuple[RuntimeEvidenceManifest | None, str]:
    raw = payload.get("runtime_evidence_manifest")
    if not isinstance(raw, dict) or not raw:
        return None, "missing_runtime_evidence_manifest"
    try:
        return RuntimeEvidenceManifest.model_validate(raw), ""
    except Exception as exc:
        return None, f"malformed_runtime_evidence_manifest:{exc}"


def _manifest_identity_mismatches(manifest: RuntimeEvidenceManifest, payload: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for field in ("request_id", "task_id", "runtime_hash"):
        value = payload.get(field)
        if value is None or value == "":
            continue
        if str(value) != str(getattr(manifest, field)):
            mismatches.append(field)
    return mismatches


def _manifest_has_repo_patch_evidence(manifest: RuntimeEvidenceManifest) -> bool:
    for event in manifest.trace_events:
        if event.node_type == "repo_patch" or event.event == "repo_patch":
            return True
    for intent in manifest.side_effect_intents:
        if intent.intent_kind == "repo_patch":
            return True
    for receipt in manifest.side_effect_receipts:
        if str(receipt.get("action_kind", "") or "") == "repo_patch":
            return True
        if str(receipt.get("node_type", "") or "") == "repo_patch":
            return True
        result_ref = receipt.get("result_ref", {})
        request = result_ref.get("request", {}) if isinstance(result_ref, dict) else {}
        if isinstance(request, dict) and str(request.get("node_type", "") or "") == "repo_patch":
            return True
    return False


def _artifact_contains_spoofed_flags(artifact: Any) -> bool:
    if not isinstance(artifact, dict):
        return False
    return bool(_ARTIFACT_FLAG_KEYS & set(artifact))


def _artifact_flag_names(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    return sorted(str(key) for key in _ARTIFACT_FLAG_KEYS & set(artifact))


def _artifact_keys(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    return sorted(str(key) for key in artifact)


def _runner_receipt(spec: ValidatorSpec, artifact: Any, payload: dict[str, Any]) -> dict[str, Any]:
    fixture = RepoPatchFixture.from_spec_inputs(spec.inputs)
    if fixture is not None:
        runner = _configured_evaluator_runner(spec.inputs)
        if runner is None:
            return {}
        result = runner.run(candidate_artifact=artifact, fixture=fixture)
        return result.model_dump(mode="json", exclude_none=True)
    receipt = payload.get("repo_patch_result") or {}
    if isinstance(receipt, dict) and receipt.get("runner_digest"):
        return dict(receipt)
    return {}


def _configured_evaluator_runner(inputs: dict[str, Any]) -> RepoPatchEvaluatorRunner | None:
    raw_config = inputs.get("evaluator_command_backend")
    if not isinstance(raw_config, dict):
        return None
    kind = str(raw_config.get("kind", "") or "").strip()
    if kind == "trusted_local_for_offline_tests":
        return RepoPatchEvaluatorRunner(TrustedLocalRepoPatchCommandBackend())
    if kind != "isolated_v1":
        return None
    raw_policy = raw_config.get("policy")
    if not isinstance(raw_policy, dict):
        return None
    try:
        policy = IsolatedCommandPolicy.model_validate(raw_policy)
        command_backend = DockerCommandBackend(policy)
        backend = IsolatedRepoPatchCommandBackend(
            command_backend,
            environment_identity={"command_policy": policy.model_dump(mode="json")},
            python_argv=tuple(raw_config.get("python_argv", ("python",))),
            git_argv=tuple(raw_config.get("git_argv", ("git",))),
        )
    except (TypeError, ValueError):
        return None
    return RepoPatchEvaluatorRunner(backend)


def _malformed_receipt_bool_fields(receipt: dict[str, Any]) -> list[str]:
    malformed: list[str] = []
    for key in ("applied", "public_tests_passed", "tampered_tests"):
        if type(receipt.get(key)) is not bool:
            malformed.append(key)
    if receipt.get("hidden_tests_passed") is not None and type(receipt.get("hidden_tests_passed")) is not bool:
        malformed.append("hidden_tests_passed")
    for key in (
        "source_snapshot_unchanged",
        "scratch_snapshot_matched",
        "fixture_identity_matched",
        "clean_copy_snapshot_unchanged",
    ):
        if key in receipt and type(receipt.get(key)) is not bool:
            malformed.append(key)
    return malformed


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    artifact = payload.get("artifact")
    manifest, manifest_reason = _manifest(payload)
    if manifest is None:
        if _artifact_contains_spoofed_flags(artifact):
            return ValidatorResult(
                validator_id=spec.validator_id,
                family_id=spec.family_id,
                claim_ids=list(spec.claim_ids),
                status="fail",
                authority_used="A0",
                health_status={"runtime_evidence_manifest": False, "evaluator_runner_receipt": False},
                observations={
                    "reason": "artifact_flag_spoof_without_runner_evidence",
                    "artifact_flags_ignored": _artifact_flag_names(artifact),
                },
            )
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"runtime_evidence_manifest": False, "evaluator_runner_receipt": False},
            observations={"reason": manifest_reason, "artifact_flags_ignored": _artifact_keys(artifact)},
        )
    identity_mismatches = _manifest_identity_mismatches(manifest, payload)
    if identity_mismatches:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"runtime_evidence_manifest": True, "manifest_identity": False, "evaluator_runner_receipt": False},
            observations={
                "reason": "runtime_evidence_manifest_identity_mismatch",
                "mismatched_fields": identity_mismatches,
                "payload_request_id": str(payload.get("request_id", "") or ""),
                "manifest_request_id": manifest.request_id,
                "payload_task_id": str(payload.get("task_id", "") or ""),
                "manifest_task_id": manifest.task_id,
                "payload_runtime_hash": str(payload.get("runtime_hash", "") or ""),
                "manifest_runtime_hash": manifest.runtime_hash,
                "manifest_id": manifest.manifest_id,
            },
        )
    if not _manifest_has_repo_patch_evidence(manifest):
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"runtime_evidence_manifest": True, "repo_patch_runtime_evidence": False, "evaluator_runner_receipt": False},
            observations={"reason": "missing_repo_patch_runtime_evidence", "manifest_id": manifest.manifest_id},
        )
    receipt = _runner_receipt(spec, artifact, payload)
    if not isinstance(receipt, dict) or not receipt.get("runner_digest"):
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="fail",
            authority_used="A0",
            health_status={"evaluator_runner_receipt": False, "test_tamper_scan": False},
            observations={"reason": "missing_evaluator_runner_receipt", "artifact_flags_ignored": _artifact_keys(artifact)},
        )
    malformed_bool_fields = _malformed_receipt_bool_fields(receipt)
    if malformed_bool_fields:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="fail",
            authority_used="A0",
            health_status={"evaluator_runner_receipt": True, "receipt_shape": False, "test_tamper_scan": False},
            observations={
                "reason": "malformed_repo_patch_receipt_booleans",
                "malformed_fields": malformed_bool_fields,
            },
        )
    hidden_passed = receipt.get("hidden_tests_passed")
    spec_hidden_digest = str(spec.inputs.get("hidden_tests_digest", "") or "")
    receipt_hidden_digest = str(receipt.get("hidden_tests_digest", "") or "")
    if hidden_passed is not None and (not spec_hidden_digest or not receipt_hidden_digest):
        missing_hidden_digests = []
        if not spec_hidden_digest:
            missing_hidden_digests.append("spec.hidden_tests_digest")
        if not receipt_hidden_digest:
            missing_hidden_digests.append("receipt.hidden_tests_digest")
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="fail",
            authority_used="A0",
            health_status={"evaluator_runner_receipt": True, "fixture_digest_bound": False, "test_tamper_scan": False},
            observations={
                "reason": "missing_hidden_tests_digest_for_sealed_evidence",
                "missing_digests": missing_hidden_digests,
            },
        )
    digest_keys = ["repo_snapshot_digest", "public_test_command_digest"]
    if spec.inputs.get("hidden_tests_digest") or hidden_passed is not None:
        digest_keys.append("hidden_tests_digest")
    digest_mismatches = [
        key
        for key in digest_keys
        if str(receipt.get(key, "") or "") != str(spec.inputs.get(key, "") or "")
    ]
    if digest_mismatches:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="fail",
            authority_used="A0",
            health_status={"evaluator_runner_receipt": True, "fixture_digest_bound": False, "test_tamper_scan": False},
            observations={"reason": "receipt_fixture_digest_mismatch", "mismatched_digests": digest_mismatches},
        )
    applied = bool(receipt.get("applied", False))
    tampered_tests = bool(receipt.get("tampered_tests", False))
    source_unchanged = bool(receipt.get("source_snapshot_unchanged", True))
    scratch_matched = bool(receipt.get("scratch_snapshot_matched", True))
    fixture_identity_matched = bool(receipt.get("fixture_identity_matched", True))
    clean_copy_unchanged = bool(receipt.get("clean_copy_snapshot_unchanged", True))
    evaluator_integrity = source_unchanged and scratch_matched and fixture_identity_matched and clean_copy_unchanged
    public_passed = receipt.get("public_tests_passed", applied)
    sealed_commands_ran = hidden_passed is not None
    passed = bool(
        applied
        and public_passed
        and not tampered_tests
        and evaluator_integrity
        and (hidden_passed is True if sealed_commands_ran else True)
    )
    status = "quarantine" if tampered_tests or not evaluator_integrity else "pass" if passed else "fail"
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status=status,
        authority_used="A4" if sealed_commands_ran else "A3",
        health_status={
            "runtime_evidence_manifest": True,
            "patch_applied": applied,
            "test_tamper_scan": not tampered_tests,
            "fixture_digest_bound": True,
            "source_snapshot_unchanged": source_unchanged,
            "scratch_snapshot_matched": scratch_matched,
            "fixture_identity_matched": fixture_identity_matched,
            "clean_copy_snapshot_unchanged": clean_copy_unchanged,
        },
        observations={
            "applied": applied,
            "public_tests_passed": public_passed,
            "hidden_tests_passed": hidden_passed,
            "tampered_tests": tampered_tests,
            "tampered_paths": list(receipt.get("tampered_paths", []) or []),
            "source_snapshot_unchanged": source_unchanged,
            "scratch_snapshot_matched": scratch_matched,
            "fixture_identity_matched": fixture_identity_matched,
            "clean_copy_snapshot_unchanged": clean_copy_unchanged,
            "patched_clean_digest": str(receipt.get("patched_clean_digest", "") or ""),
            "workspace_drift_evidence": list(receipt.get("workspace_drift_evidence", []) or []),
            "execution_backend_id": str(receipt.get("execution_backend_id", "") or ""),
            "execution_backend_digest": str(receipt.get("execution_backend_digest", "") or ""),
            "runner_digest": str(receipt.get("runner_digest", "") or ""),
            "manifest_id": manifest.manifest_id,
            "command_digests": {
                "public": str(receipt.get("public_test_command_digest", "") or ""),
                "hidden": str(receipt.get("hidden_tests_digest", "") or ""),
            },
            "log_digests": [
                str(row.get("log_digest", "") or "")
                for row in [*list(receipt.get("public_command_results", []) or []), *list(receipt.get("hidden_command_results", []) or [])]
                if isinstance(row, dict)
            ],
            "environment_digest": str(receipt.get("environment_digest", "") or ""),
            "fixture_digest": str(receipt.get("fixture_digest", "") or ""),
            "evaluation_contract_digest": str(receipt.get("evaluation_contract_digest", "") or ""),
        },
    )


def _applicability(context: dict[str, Any]) -> float:
    if not context.get("repo_patch_fixture_available"):
        return 0.0
    goal = str(context.get("goal_text", "")).lower()
    return 0.95 if any(word in goal for word in ["repo", "patch", "code", "test", "bug", "swe"]) else 0.05


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="repo_patch",
        description="SWE-bench-style patch validator: applies repo patches, runs public/hidden tests, and checks test tampering.",
        authority_ceiling="A4",
        default_visibility="sealed",
        leakage_risk="medium",
        default_failure_action="reject",
        input_contract={
            "requires": ["patch_artifact", "repo_snapshot_digest", "public_test_command_digest"],
            "sealed_optional": ["hidden_tests_digest"],
            "runtime_payload_requires": ["repo_patch_result.runner_digest"],
        },
        output_schema={"type": "object", "properties": {"public_tests_passed": {"type": "boolean"}, "hidden_tests_passed": {"type": "boolean"}}},
        run=_run,
        applicability=_applicability,
    )
