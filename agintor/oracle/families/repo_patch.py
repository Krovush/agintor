from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    artifact = payload.get("artifact") or {}
    if not isinstance(artifact, dict):
        artifact = {}
    receipt = payload.get("repo_patch_result") or {}
    if not isinstance(receipt, dict) or not receipt.get("runner_digest"):
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="fail",
            authority_used="A0",
            health_status={"evaluator_runner_receipt": False, "test_tamper_scan": False},
            observations={"reason": "missing_evaluator_runner_receipt", "artifact_flags_ignored": sorted(artifact)},
        )
    digest_keys = ("repo_snapshot_digest", "public_test_command_digest", "hidden_tests_digest")
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
    hidden_passed = receipt.get("hidden_tests_passed")
    public_passed = receipt.get("public_tests_passed", applied)
    passed = bool(applied and public_passed and hidden_passed is True and not tampered_tests)
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A4" if hidden_passed is not None else "A3",
        health_status={"patch_applied": applied, "test_tamper_scan": not tampered_tests},
        observations={"applied": applied, "public_tests_passed": public_passed, "hidden_tests_passed": hidden_passed, "tampered_tests": tampered_tests},
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
            "sealed_requires": ["hidden_tests_digest"],
            "runtime_payload_requires": ["repo_patch_result.runner_digest"],
        },
        output_schema={"type": "object", "properties": {"public_tests_passed": {"type": "boolean"}, "hidden_tests_passed": {"type": "boolean"}}},
        run=_run,
        applicability=_applicability,
    )
