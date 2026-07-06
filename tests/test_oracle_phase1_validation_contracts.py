from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.contracts import (
    EvidenceLedger,
    GoalSpec,
    RunResult,
    ValidationPlan,
    ValidatorReport,
    baseline_langgraph_runtime_spec,
    validation_plan_from_oracle_package,
    validation_plan_hash,
)
from agintor.evaluation.oracle_runner import OracleEvaluationRunner
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.package_io import load_oracle_package, write_oracle_package


def _schema_goal(goal_id: str = "goal.phase1") -> GoalSpec:
    return GoalSpec(
        goal_id=goal_id,
        raw_prompt="return a JSON report",
        normalized_goal="return a JSON report",
        constraints={"artifact_schema": {"required": ["answer"]}},
    )


def _compiled_package(goal_id: str = "goal.phase1"):
    return OracleCompiler().compile(
        _schema_goal(goal_id),
        baseline_langgraph_runtime_spec(runtime_id=f"runtime.{goal_id}"),
    )


def test_validation_plan_round_trips_and_hash_stays_stable() -> None:
    package = _compiled_package()
    plan = package.validation_plan or validation_plan_from_oracle_package(package)
    first_hash = validation_plan_hash(plan)

    current = plan
    for _ in range(3):
        payload = json.loads(json.dumps(current.model_dump(mode="json", exclude_none=True), sort_keys=True))
        current = ValidationPlan.model_validate(payload)
        assert current == ValidationPlan.model_validate(current.model_dump(mode="json", exclude_none=True))
        assert validation_plan_hash(current) == first_hash


def test_compiled_oracle_package_persists_validation_plan_hash(tmp_path: Path) -> None:
    package = _compiled_package("goal.phase1.persist")
    plan = package.validation_plan or validation_plan_from_oracle_package(package)

    assert package.validation_plan_hash
    assert plan.public_projection_hash == package.public_view_hash
    assert plan.sealed_projection_hash == package.sealed_view_hash
    assert plan.validator_bundle_hash
    assert plan.fixture_digests
    assert validation_plan_hash(plan) == package.validation_plan_hash

    written = write_oracle_package(package, tmp_path / "oracle")
    loaded = load_oracle_package(tmp_path / "oracle")

    assert written.validation_plan_hash == package.validation_plan_hash
    assert loaded.validation_plan_hash == package.validation_plan_hash
    assert validation_plan_hash(validation_plan_from_oracle_package(loaded)) == package.validation_plan_hash


def test_validation_plan_covers_every_task_claim_or_records_residual() -> None:
    package = _compiled_package("goal.phase1.coverage")
    plan = package.validation_plan or validation_plan_from_oracle_package(package)
    claim_by_id = {claim.claim_id: claim for claim in plan.claims}

    for task_set in package.task_sets:
        for task in task_set.tasks:
            assert task.claim_ids
            for claim_id in task.claim_ids:
                claim = claim_by_id[str(claim_id)]
                assert claim.proof_obligation_ids or claim.residual_reason or plan.residuals.get(claim.claim_id)


def test_oracle_runner_emits_validator_reports_and_evidence_ledger() -> None:
    package = _compiled_package("goal.phase1.ledger")
    task = package.task_sets[0].tasks[0]
    run = RunResult(
        run_id="run.phase1",
        runtime_hash="runtime",
        task_id=task.task_id,
        seed=0,
        artifact={"answer": "ok"},
        verifier_score=0.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace=[{"event": "langgraph_node_completed"}],
    )

    validator_results, claim_results, ledger = OracleEvaluationRunner().evaluate_run_with_ledger(package, run)

    assert validator_results
    assert claim_results
    assert ledger.validation_plan_hash == package.validation_plan_hash
    assert ledger.validator_reports
    assert ledger.claim_posteriors
    assert all(report.report_id for report in ledger.validator_reports)
    assert all(posterior.validator_report_ids or posterior.residual_reason for posterior in ledger.claim_posteriors)


def test_validator_report_identity_binds_authority_ceiling() -> None:
    base = {
        "validator_id": "validator.identity",
        "family_id": "stage4_identity",
        "claim_ids": ["claim.identity"],
        "status": "pass",
        "score": 1.0,
        "interval_lower": 1.0,
        "interval_upper": 1.0,
        "authority_used": "A5",
        "coverage": 1.0,
        "independence_group": "identity",
        "observations": {"ok": True},
    }
    capped = ValidatorReport(**base, authority_ceiling="A3")
    uncapped = ValidatorReport(**base, authority_ceiling="A5")
    capped_payload = capped.model_dump(mode="json", exclude_none=True)
    capped_payload.pop("report_id")

    assert capped.report_id != uncapped.report_id
    assert ValidatorReport.model_validate(capped_payload).report_id == capped.report_id


def test_evidence_ledger_rejects_promotion_authoritative_scalar_without_validator_reports() -> None:
    with pytest.raises(ValueError, match="promotion-authoritative scalar scores require validator reports"):
        EvidenceLedger(
            runtime_hash="runtime",
            task_id="task",
            scalar_score=1.0,
            scalar_score_authority="M5",
            promotion_authoritative=True,
        )
