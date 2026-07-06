from __future__ import annotations

import json
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from agintor.contracts import (
    AxisDelta,
    BenchmarkTask,
    CapabilityExchange,
    ClaimPosterior,
    ClaimGraph,
    ClaimResult,
    ClaimSpec,
    DomainEvidenceContract,
    EvidenceLedger,
    GoalSpec,
    OraclePackage,
    OracleTask,
    OracleTaskSet,
    PairedComparison,
    PromotionDecision,
    RunResult,
    RuntimeBatchResponse,
    ScoringProjection,
    SpecAction,
    SuiteEvaluation,
    TaskScore,
    ValidationIntent,
    ValidatorResult,
    ValidatorSpec,
    baseline_langgraph_runtime_spec,
)
from agintor.contracts.runtime_spec import RuntimeSpec
from agintor.evaluation.benchmarks import load_suite
from agintor.evaluation.evaluator import RuntimeEvaluator
from agintor.evaluation.oracle_runner import OracleEvaluationRunner
from agintor.evaluation.progress_oracle import ProgressOracle
from agintor.evaluation.scoring import ScoreCalculator
from agintor.integrations.tradingagents.compiler import tradingagents_spec_from_goal
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.package_io import assert_package_lock_matches, load_oracle_package, write_oracle_package
from agintor.oracle.projections import public_oracle_projection
from agintor.oracle.qa import OracleQARunner
from agintor.oracle.validator_registry import ValidatorFamily, ValidatorRegistry
from agintor.providers import LocalDeterministicProvider
from agintor.runtime.api import load_solve_request, runtime_solve_request_for_user_request
from agintor.runtime.host import RuntimeHost
from agintor.runtime.langgraph.adapters import run_spec_task
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.langgraph.executor import compile_runtime_spec
from agintor.runtime.loader import load_runtime, runtime_identity_inputs
from agintor.runtime.profile import load_runtime_profile
from agintor.runtime.project import init_runtime
from agintor.search.engine import EvolutionEngine
from agintor.search.spec_mutator import HeuristicSpecActionMutator, SpecMutationContext


def _goal() -> GoalSpec:
    return GoalSpec(
        goal_id="goal.pass1",
        raw_prompt="build a repo patch assistant",
        normalized_goal="build a repo patch assistant",
    )


def _contract() -> DomainEvidenceContract:
    return DomainEvidenceContract(
        contract_id="oracle-contract.pass1",
        domain_kind="validation_backed_runtime",
        version="oracle",
        scope={"domain": "validation_backed_runtime"},
        challenge_distribution={"minimum_frontier_tasks": 1},
        answer_mechanism={"type": "oracle_package"},
        quality_axes=[
            {
                "axis_id": "claim.goal_outcome",
                "promotion_kind": "capability",
                "comparator_type": "hidden_challenge",
                "minimum_authority": "A4",
            }
        ],
        health_floors={"oracle_package_qa": "pass", "leakage": "pass"},
    )


def test_runtime_spec_digest_is_stable_and_rejects_private_keys() -> None:
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.pass1")
    payload = json.loads(json.dumps(spec.model_dump(mode="json"), sort_keys=True))

    assert RuntimeSpec.model_validate(payload).spec_digest == spec.spec_digest
    assert spec.model_copy(update={"metadata": {"note": "ignored"}}, deep=True).spec_digest == spec.spec_digest

    with pytest.raises(ValueError, match="private/sealed key"):
        SpecAction(
            action_id="spec-action.bad",
            action_type="set_prompt",
            target_ids=["agent.default"],
            scope=["top"],
            patch={"private_expected": "answer"},
        )


def test_oracle_package_public_projection_and_hash_roundtrip(tmp_path: Path) -> None:
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.pass1")
    package = OracleCompiler().compile(_goal(), spec)
    written = write_oracle_package(package, tmp_path / "oracle")
    loaded = load_oracle_package(tmp_path / "oracle")
    public_payload = public_oracle_projection(loaded)

    assert loaded.package_hash == written.package_hash
    assert OracleQARunner().run(loaded).passed
    public_text = json.dumps(public_payload, sort_keys=True)
    assert "private_expected" not in public_text
    assert "sealed_validators" not in public_text
    assert "validator_ids" not in json.dumps(public_payload["proof_obligations"], sort_keys=True)
    for validator in written.validator_specs:
        if validator.visibility != "public":
            assert validator.validator_id not in public_text
    assert all(
        task["benchmark_task"]["verifier_type"] == "oracle_package"
        for task_set in public_payload["task_sets"]
        for task in task_set["tasks"]
    )


def test_oracle_package_load_and_lock_recompute_validation_plan_hash(tmp_path: Path) -> None:
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.pass1.validation-hash")
    package_dir = tmp_path / "oracle"
    package = write_oracle_package(OracleCompiler().compile(_goal(), spec), package_dir)

    sealed_path = package_dir / "sealed.json"
    sealed_payload = json.loads(sealed_path.read_text(encoding="utf-8"))
    sealed_payload["validation_plan_hash"] = "tampered-validation-plan-hash"
    sealed_path.write_text(json.dumps(sealed_payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="validation_plan_hash"):
        load_oracle_package(package_dir)

    package = write_oracle_package(package, package_dir)
    sealed_payload = json.loads(sealed_path.read_text(encoding="utf-8"))
    sealed_payload.pop("validation_plan_hash")
    sealed_path.write_text(json.dumps(sealed_payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="validation_plan_hash"):
        load_oracle_package(package_dir)

    package = write_oracle_package(package, package_dir)
    sealed_payload = json.loads(sealed_path.read_text(encoding="utf-8"))
    sealed_payload["validation_plan_hash"] = ""
    sealed_path.write_text(json.dumps(sealed_payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="validation_plan_hash"):
        load_oracle_package(package_dir)

    package = write_oracle_package(package, package_dir)
    lock_path = package_dir / "manifest.json"
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_payload["validation_plan_hash"] = "tampered-validation-plan-hash"
    lock_path.write_text(json.dumps(lock_payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="validation_plan_hash"):
        assert_package_lock_matches(package, package_dir)


def test_inspect_oracle_rejects_sealed_stdout_projection(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agintor.cli import app

    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.inspect")
    write_oracle_package(OracleCompiler().compile(_goal(), spec), tmp_path / "oracle")

    public = CliRunner().invoke(app, ["inspect-oracle", str(tmp_path / "oracle")])
    assert public.exit_code == 0, public.output
    assert "private_expected" not in public.output
    assert "sealed_validators" not in public.output

    sealed = CliRunner().invoke(app, ["inspect-oracle", str(tmp_path / "oracle"), "--sealed"])
    assert sealed.exit_code != 0
    assert "private_expected" not in sealed.output
    assert "sealed_validators" not in sealed.output
    assert "host_validator_authority" not in sealed.output


def test_oracle_compiler_goal_text_prefers_goal_spec_prompt_fields() -> None:
    goal = GoalSpec(
        goal_id="goal.text",
        raw_prompt="raw repo prompt",
        normalized_goal="normalized repo prompt",
    )

    assert OracleCompiler._goal_text(goal) == "normalized repo prompt\nraw repo prompt"


def test_compile_oracle_cli_uses_stable_goal_identity_and_goal_text(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agintor.cli import app
    from agintor.utils import stable_hash

    goal_text = "  build a repo patch assistant  "
    first_dir = tmp_path / "oracle-a"
    second_dir = tmp_path / "oracle-b"
    runner = CliRunner()

    first = runner.invoke(app, ["compile-oracle", goal_text, str(first_dir)])
    second = runner.invoke(app, ["compile-oracle", goal_text, str(second_dir)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_payload = json.loads(first.output)
    assert first_payload["runtime_kind"] == "langgraph_spec"

    first_package = load_oracle_package(first_dir)
    second_package = load_oracle_package(second_dir)
    expected_goal_id = f"goal.{stable_hash('build a repo patch assistant')[:12]}"

    assert first_package.goal_id == expected_goal_id
    assert second_package.goal_id == expected_goal_id
    assert first_package.package_id == second_package.package_id
    assert first_package.runtime_spec_digest == second_package.runtime_spec_digest
    task_prompt = first_package.task_sets[0].tasks[0].benchmark_task.prompt
    assert "build a repo patch assistant" in task_prompt
    assert "raw_prompt" not in task_prompt


def test_init_runtime_rejects_goal_scoped_tradingagents_without_goal(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agintor.cli import app

    destination = tmp_path / "trade-runtime"
    result = CliRunner().invoke(
        app,
        ["init-runtime", str(destination), "--runtime-kind", "tradingagents_langgraph"],
    )

    assert result.exit_code != 0
    assert "requires a goal-scoped spec" in result.output
    assert not destination.exists()


def test_spec_backed_runtime_solves_through_host_without_resume_claims(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.host-smoke"),
        runtime_dir,
        force=True,
    )
    profile = load_runtime_profile()
    loaded = load_runtime(runtime_dir, runtime_profile=profile, runtime_backend="local")

    assert loaded.capability_exchange.checkpoint_support is False
    assert loaded.capability_exchange.resume_support is False
    assert loaded.capability_exchange.runtime_asset_capabilities["traces"] is True

    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="hello langgraph"),
    )
    response = RuntimeHost(tmp_path / "host", runtime_backend="local", artifact_mode="none").solve(
        runtime_dir,
        request,
        provider=LocalDeterministicProvider(),
        runtime_profile=profile,
    )

    assert response.solve_result.artifact == "hello langgraph"
    assert response.solve_result.run_lifecycle_state == "completed"
    assert response.solve_result.run_resumable is False


def test_progress_oracle_quarantines_oracle_hash_mismatch() -> None:
    comparison = PairedComparison(
        comparison_id="cmp",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="oracle-contract.pass1",
        parent_oracle_package_hash="oracle-a",
        child_oracle_package_hash="oracle-b",
        challenge_ids=["challenge"],
        axis_deltas={
            "claim.goal_outcome": AxisDelta(
                axis_id="claim.goal_outcome",
                estimate=1.0,
                lower=1.0,
                upper=1.0,
                evidence_count=1,
                authority_level="A4",
                source="hidden_frontier",
            )
        },
        health_floor_status={"oracle_package_qa": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(), comparison=comparison)

    assert decision.decision_type == "quarantine"
    assert "oracle_package_hash_mismatch" in decision.reason_codes


def test_oracle_evidence_rows_carry_validator_claim_and_spec_identity(tmp_path: Path) -> None:
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.evidence")
    package = OracleCompiler().compile(_goal(), spec)
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package
    evaluator.oracle_runner = OracleEvaluationRunner()
    run = RunResult(
        runtime_hash="runtime",
        task_id=package.task_sets[0].tasks[0].task_id,
        seed=0,
        artifact="answer",
        verifier_score=0.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace=[{"event": "langgraph_node_completed", "node_id": "node.default"}],
    )
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={"s:oracle": 0.0},
        task_scores={
            run.task_id: TaskScore(
                s=0.0,
                rho=0.0,
                cvar=0.0,
                utilities=[0.0],
                verifier_scores=[0.0],
                costs=[0.0],
                latencies=[0.0],
                faults=[0],
            )
        },
        family_scores={"e2e": {"s": 0.0}},
        run_results=[run],
        evaluation_identity={"runtime_spec_digest": spec.spec_digest},
    )
    decision = PromotionDecision(
        decision_id="decision",
        decision_type="no_progress",
        contract_id=package.evidence_contract.contract_id,
        parent_runtime_hash="parent",
        child_runtime_hash="runtime",
        oracle_package_hash=package.package_hash,
        child_runtime_spec_digest=spec.spec_digest,
        comparison_ref="comparison",
        reason_codes=["quality_lcb_not_cleared"],
    )

    rows = evaluator._stage4_evidence_rows(evaluation, role="child", decision=decision)

    assert "__agintor_evaluation_identity__" not in evaluation.task_metadata
    assert rows[0]["oracle_package_hash"] == package.package_hash
    assert rows[0]["runtime_spec_digest"] == spec.spec_digest
    assert rows[0]["validator_results"]
    assert rows[0]["claim_results"]
    assert rows[0]["evidence_digest"]


def test_stage4_evidence_rows_score_from_ledger_posteriors_not_raw_claim_results() -> None:
    run = RunResult(
        runtime_hash="runtime",
        task_id="oracle.stage4-ledger.posterior-score.train.0",
        run_id="run.posterior-score",
        seed=0,
        artifact={"answer": "ok"},
        verifier_score=1.0,
        cost=0.0,
        latency=0.0,
        faults=0,
    )
    raw_claim = ClaimResult(
        claim_id="claim.posterior_score",
        satisfied=True,
        posterior_lower=1.0,
        posterior_upper=1.0,
        authority_mass={"A5": 1.0},
        coverage=1.0,
        validator_result_ids=["validator.posterior-score"],
        evidence_digest="raw-claim-digest",
    )
    posterior = ClaimPosterior(
        claim_id=raw_claim.claim_id,
        state="abstained",
        authority_mass={"A3": 1.0},
        coverage=0.0,
        residual_mass=1.0,
        residual_reason="insufficient_authority",
        validator_report_ids=["report.posterior-score"],
    )
    ledger = EvidenceLedger(
        oracle_package_hash="oracle-package.posterior-score",
        validation_plan_hash="validation-plan.posterior-score",
        runtime_hash=run.runtime_hash,
        task_id=run.task_id,
        run_id=run.run_id,
        seed=run.seed,
        claim_posteriors=[posterior],
        authority_mass={"A3": 1.0},
        coverage={posterior.claim_id: 0.0},
        unverifiable_residual={posterior.claim_id: posterior.residual_reason},
        audit_status="abstain",
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = SimpleNamespace(
        package_hash=ledger.oracle_package_hash,
        validation_plan_hash=ledger.validation_plan_hash,
        public_view_hash="public",
        sealed_view_hash="sealed",
        scoring_projection=SimpleNamespace(hard_claim_ids=[], claim_weights={}),
    )
    evaluator._oracle_task_by_runtime_task_id = lambda *_args, **_kwargs: {
        run.task_id: SimpleNamespace(claim_ids=[raw_claim.claim_id])
    }
    evaluator._oracle_evidence_for_run_with_ledger = lambda _run, oracle_task=None: ([], [raw_claim], ledger)
    evaluation = SuiteEvaluation(
        runtime_hash=run.runtime_hash,
        objective_scores={},
        task_scores={},
        family_scores={},
        run_results=[run],
    )
    decision = PromotionDecision(
        decision_id="decision.posterior-score",
        decision_type="no_progress",
        contract_id="contract.posterior-score",
        parent_runtime_hash="parent",
        child_runtime_hash=run.runtime_hash,
        oracle_package_hash=ledger.oracle_package_hash,
        comparison_ref="comparison.posterior-score",
        reason_codes=["quality_lcb_not_cleared"],
    )

    row = evaluator._stage4_evidence_rows(evaluation, role="child", decision=decision)[0]

    assert row["claim_results"][0]["satisfied"] is True
    assert row["evidence_ledger"]["claim_posteriors"][0]["state"] == "abstained"
    assert row["verifier_evidence"][0]["oracle_axis_scores"] == {raw_claim.claim_id: 0.0}
    assert row["axis_scores"][0]["axis_id"] == raw_claim.claim_id
    assert row["axis_scores"][0]["score"] == 0.0
    assert row["axis_scores"][0]["authority"] == "A3"
    assert evaluator._oracle_axis_scores_for_run(
        run,
        SimpleNamespace(claim_ids=[raw_claim.claim_id]),
    ) == {raw_claim.claim_id: 0.0}
    rescored = evaluator._score_oracle_results([run], partition="train", tasks=[])
    assert rescored[0].verifier_score == 0.0


def _stage4_ledger_package_for_validator(validator: ValidatorSpec) -> OraclePackage:
    claim = ClaimSpec(
        claim_id="claim.stage4_ledger",
        text="Stage 4 ledger claim.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A3",
    )
    task = BenchmarkTask(
        task_id=f"oracle.stage4-ledger.{validator.family_id}.train.0",
        family="e2e",
        prompt="Produce an artifact.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    return OraclePackage(
        package_id=f"oracle-package.stage4-ledger.{validator.family_id}",
        goal_id=f"goal.stage4-ledger.{validator.family_id}",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[validator.model_copy(update={"claim_ids": [claim.claim_id]}, deep=True)],
        task_sets=[
            OracleTaskSet(
                task_set_id=f"oracle-taskset.stage4-ledger.{validator.family_id}.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[claim.claim_id],
                        validator_ids=[validator.validator_id],
                    )
                ],
            )
        ],
        evidence_contract=_contract(),
    )


def test_stage4_evidence_ledger_audit_status_requires_supporting_reports() -> None:
    abstain_package = _stage4_ledger_package_for_validator(
        ValidatorSpec(
            validator_id="validator.schema.abstain",
            family_id="schema_artifact",
            inputs={},
            visibility="sealed",
        )
    )
    abstain_task = abstain_package.task_sets[0].tasks[0]

    _abstain_results, _abstain_claims, abstain_ledger = OracleEvaluationRunner().evaluate_run_with_ledger(
        abstain_package,
        {
            "task_id": abstain_task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )

    def raising_validator(_spec: ValidatorSpec, _payload: dict[str, object]):
        raise RuntimeError("validator boom")

    error_validator = ValidatorSpec(
        validator_id="validator.error",
        family_id="stage4_error",
        inputs={},
        visibility="sealed",
    )
    error_package = _stage4_ledger_package_for_validator(error_validator)
    error_task = error_package.task_sets[0].tasks[0]
    registry = ValidatorRegistry(
        [
            ValidatorFamily(
                family_id="stage4_error",
                description="Raises for audit-status regression coverage.",
                run=raising_validator,
            )
        ]
    )

    _error_results, _error_claims, error_ledger = OracleEvaluationRunner(registry).evaluate_run_with_ledger(
        error_package,
        {
            "task_id": error_task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )
    covered_claim = ClaimSpec(
        claim_id="claim.covered",
        text="Covered claim.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A3",
    )
    residual_claim = ClaimSpec(
        claim_id="claim.residual",
        text="Unverified residual claim.",
        claim_type="process",
        criticality="major",
        weight=1.0,
        minimum_authority="A3",
    )
    partial_validator = ValidatorSpec(
        validator_id="validator.partial.schema",
        family_id="schema_artifact",
        claim_ids=[covered_claim.claim_id],
        inputs={"schema": {"type": "object", "required": ["answer"]}},
        visibility="sealed",
    )
    partial_task = BenchmarkTask(
        task_id="oracle.stage4-ledger.partial.train.0",
        family="e2e",
        prompt="Produce an artifact.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    partial_package = OraclePackage(
        package_id="oracle-package.stage4-ledger.partial",
        goal_id="goal.stage4-ledger.partial",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[covered_claim, residual_claim]),
        validator_specs=[partial_validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.stage4-ledger.partial.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=partial_task.task_id,
                        benchmark_task=partial_task,
                        claim_ids=[covered_claim.claim_id, residual_claim.claim_id],
                        validator_ids=[partial_validator.validator_id],
                    )
                ],
            )
        ],
        evidence_contract=_contract(),
    )
    _partial_results, _partial_claims, partial_ledger = OracleEvaluationRunner().evaluate_run_with_ledger(
        partial_package,
        {
            "task_id": partial_task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )

    assert abstain_ledger.validator_reports[0].status == "abstain"
    assert abstain_ledger.validator_reports[0].coverage == 0.0
    assert abstain_ledger.claim_posteriors[0].state == "abstained"
    assert abstain_ledger.claim_posteriors[0].authority_mass == {}
    assert abstain_ledger.claim_posteriors[0].coverage == 0.0
    assert abstain_ledger.claim_posteriors[0].residual_mass == 1.0
    assert abstain_ledger.coverage[abstain_ledger.claim_posteriors[0].claim_id] == 0.0
    assert abstain_ledger.audit_status == "abstain"
    assert error_ledger.validator_reports[0].status == "error"
    assert error_ledger.validator_reports[0].coverage == 0.0
    assert error_ledger.claim_posteriors[0].coverage == 0.0
    assert error_ledger.claim_posteriors[0].residual_mass == 1.0
    assert error_ledger.coverage[error_ledger.claim_posteriors[0].claim_id] == 0.0
    assert error_ledger.audit_status == "fail"
    partial_posteriors = {posterior.claim_id: posterior for posterior in partial_ledger.claim_posteriors}
    assert partial_posteriors[covered_claim.claim_id].coverage == 1.0
    assert partial_posteriors[residual_claim.claim_id].coverage == 0.0
    assert partial_posteriors[residual_claim.claim_id].residual_reason == "missing_validator_result"
    assert partial_ledger.unverifiable_residual == {residual_claim.claim_id: "missing_validator_result"}
    assert partial_ledger.audit_status == "abstain"


def test_stage4_evidence_ledger_quarantines_leaky_satisfied_validator_reports() -> None:
    def leaky_validator(spec: ValidatorSpec, _payload: dict[str, object]) -> ValidatorResult:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="pass",
            authority_used="A4",
            observations={"leakage": True, "leakage_flags": ["sealed_prompt_leak"]},
        )

    validator = ValidatorSpec(
        validator_id="validator.leaky",
        family_id="stage4_leaky",
        visibility="sealed",
    )
    package = _stage4_ledger_package_for_validator(validator)
    task = package.task_sets[0].tasks[0]
    registry = ValidatorRegistry(
        [
            ValidatorFamily(
                family_id="stage4_leaky",
                description="Returns passing but leaky evidence for quarantine regression coverage.",
                run=leaky_validator,
            )
        ]
    )

    validator_results, claim_results, ledger = OracleEvaluationRunner(registry).evaluate_run_with_ledger(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )

    posterior = ledger.claim_posteriors[0]
    assert validator_results[0].status == "pass"
    assert claim_results[0].satisfied is True
    assert ledger.validator_reports[0].leakage_flags == ["sealed_prompt_leak"]
    assert posterior.state == "quarantined"
    assert posterior.coverage == 0.0
    assert posterior.residual_mass == 1.0
    assert posterior.residual_reason == "leakage_flag"
    assert ledger.audit_status == "quarantine"
    assert RuntimeEvaluator._oracle_axis_score_from_claim_posterior(posterior) == 0.0


def test_stage4_evidence_ledger_abstains_when_satisfied_claim_lacks_authority_floor() -> None:
    claim = ClaimSpec(
        claim_id="claim.under_authority",
        text="A4 claim covered only by an A3 schema validator.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )
    validator = ValidatorSpec(
        validator_id="validator.under_authority.schema",
        family_id="schema_artifact",
        claim_ids=[claim.claim_id],
        inputs={"schema": {"type": "object", "required": ["answer"]}},
        authority_ceiling="A3",
        visibility="sealed",
    )
    task = BenchmarkTask(
        task_id="oracle.stage4-ledger.under-authority.train.0",
        family="e2e",
        prompt="Produce an artifact.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    package = OraclePackage(
        package_id="oracle-package.stage4-ledger.under-authority",
        goal_id="goal.stage4-ledger.under-authority",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.stage4-ledger.under-authority.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[claim.claim_id],
                        validator_ids=[validator.validator_id],
                    )
                ],
            )
        ],
        evidence_contract=_contract(),
    )

    validator_results, claim_results, ledger = OracleEvaluationRunner().evaluate_run_with_ledger(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )

    posterior = ledger.claim_posteriors[0]
    assert validator_results[0].status == "pass"
    assert validator_results[0].authority_used == "A3"
    assert claim_results[0].satisfied is True
    assert posterior.state == "abstained"
    assert posterior.authority_mass == {"A3": 1.0}
    assert posterior.coverage == 0.0
    assert posterior.residual_mass == 1.0
    assert posterior.residual_reason == "insufficient_authority"
    assert ledger.unverifiable_residual == {claim.claim_id: "insufficient_authority"}
    assert ledger.audit_status == "abstain"


def test_stage4_evidence_ledger_clamps_validator_authority_to_spec_ceiling() -> None:
    claim = ClaimSpec(
        claim_id="claim.capped_authority",
        text="A4 claim cannot be satisfied by evidence capped below A4.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )

    def inflated_authority_validator(spec: ValidatorSpec, _payload: dict[str, object]) -> ValidatorResult:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="pass",
            authority_used="A5",
            observations={"source": "inflated_authority_regression"},
        )

    validator = ValidatorSpec(
        validator_id="validator.capped-authority",
        family_id="stage4_capped_authority",
        claim_ids=[claim.claim_id],
        authority_ceiling="A3",
        visibility="sealed",
    )
    task = BenchmarkTask(
        task_id="oracle.stage4-ledger.capped-authority.train.0",
        family="e2e",
        prompt="Produce an artifact.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    package = OraclePackage(
        package_id="oracle-package.stage4-ledger.capped-authority",
        goal_id="goal.stage4-ledger.capped-authority",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.stage4-ledger.capped-authority.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[claim.claim_id],
                        validator_ids=[validator.validator_id],
                    )
                ],
            )
        ],
        evidence_contract=_contract(),
    )
    registry = ValidatorRegistry(
        [
            ValidatorFamily(
                family_id="stage4_capped_authority",
                description="Returns inflated authority for ceiling-clamp regression coverage.",
                run=inflated_authority_validator,
            )
        ]
    )

    validator_results, claim_results, ledger = OracleEvaluationRunner(registry).evaluate_run_with_ledger(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )

    posterior = ledger.claim_posteriors[0]
    assert validator_results[0].status == "pass"
    assert validator_results[0].authority_used == "A5"
    assert ledger.validator_reports[0].authority_used == "A5"
    assert ledger.validator_reports[0].authority_ceiling == "A3"
    assert claim_results[0].satisfied is True
    assert claim_results[0].authority_mass == {"A5": 1.0}
    assert posterior.state == "abstained"
    assert posterior.authority_mass == {"A3": 1.0}
    assert posterior.coverage == 0.0
    assert posterior.residual_reason == "insufficient_authority"
    posterior_payload = posterior.model_dump(mode="json", exclude_none=True)
    posterior_payload.pop("evidence_digest")
    assert posterior.evidence_digest != claim_results[0].evidence_digest
    assert posterior.__class__.model_validate(posterior_payload).evidence_digest == posterior.evidence_digest
    assert ledger.authority_mass == {"A3": 1.0}
    assert ledger.audit_status == "abstain"


def test_stage4_evidence_ledger_does_not_pass_without_claim_posteriors() -> None:
    validator = ValidatorSpec(
        validator_id="validator.no-claims.schema",
        family_id="schema_artifact",
        claim_ids=[],
        inputs={"schema": {"type": "object", "required": ["answer"]}},
        visibility="sealed",
    )
    task = BenchmarkTask(
        task_id="oracle.stage4-ledger.no-claims.train.0",
        family="e2e",
        prompt="Produce an artifact.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    package = OraclePackage(
        package_id="oracle-package.stage4-ledger.no-claims",
        goal_id="goal.stage4-ledger.no-claims",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[]),
        validator_specs=[validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.stage4-ledger.no-claims.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[],
                        validator_ids=[validator.validator_id],
                    )
                ],
            )
        ],
        evidence_contract=_contract(),
    )

    validator_results, claim_results, ledger = OracleEvaluationRunner().evaluate_run_with_ledger(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "seed": 0,
            "artifact": {"answer": "ok"},
            "verifier_score": 0.0,
        },
    )

    assert validator_results[0].status == "pass"
    assert claim_results == []
    assert ledger.validator_reports[0].status == "pass"
    assert ledger.claim_posteriors == []
    assert ledger.audit_status == "diagnostic"


def _single_exact_oracle_package() -> OraclePackage:
    claim = ClaimSpec(
        claim_id="claim.goal_outcome",
        text="Runtime returns the sealed answer.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )
    validator = ValidatorSpec(
        validator_id="validator.exact",
        family_id="exact_private_answer",
        claim_ids=[claim.claim_id],
        visibility="sealed",
        failure_action="reject",
    )
    task = BenchmarkTask(
        task_id="oracle.scoring.train.0",
        family="e2e",
        prompt="Return the public answer.",
        task_type="oracle_public_task",
        expected=None,
        private_expected="sealed-answer",
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"], "expected_digest": "sealed"},
    )
    return OraclePackage(
        package_id="oracle-package.scoring",
        goal_id="goal.scoring",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.scoring.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[claim.claim_id],
                        validator_ids=[validator.validator_id],
                        partition="train",
                    )
                ],
            )
        ],
        evidence_contract=_contract(),
        scoring_projection=ScoringProjection(
            claim_weights={claim.claim_id: 1.0},
            hard_claim_ids=[claim.claim_id],
        ),
    )


def test_oracle_validator_score_is_applied_before_suite_scoring(tmp_path: Path) -> None:
    package = _single_exact_oracle_package()
    captured: dict[str, object] = {}
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.suite = SimpleNamespace(
        all_tasks=lambda _partition: [
            package.task_sets[0].tasks[0].benchmark_task.model_copy(update={"task_id": "suite.train.different"})
        ],
        train=[],
        proxy=[],
    )
    evaluator.cache = {}
    evaluator.provider = object()
    evaluator.budget_overrides = {}
    evaluator.trace_context = None
    evaluator.predictors = SimpleNamespace(freeze=lambda: None, unfreeze=lambda: None)
    evaluator.oracle_package = package
    evaluator.oracle_runner = OracleEvaluationRunner()
    evaluator._effective_runtime_profile = lambda _runtime_dir: SimpleNamespace()
    evaluator._load_runtime = lambda _runtime_dir, runtime_profile=None: SimpleNamespace(runtime_hash="runtime")
    evaluator._score_calculator = lambda use_reference_scales=True: ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})

    class FakeRuntimeHost:
        def run_batch(self, runtime_dir, task_runs, **kwargs):
            captured["task_runs"] = task_runs
            public_task = task_runs[0][0]
            return RuntimeBatchResponse(
                request_id="batch",
                capability_exchange=CapabilityExchange(runtime_contract_version="test"),
                run_results=[
                    RunResult(
                        runtime_hash="runtime",
                        task_id=public_task.task_id,
                        seed=task_runs[0][1],
                        artifact="sealed-answer",
                        verifier_score=0.0,
                        cost=0.0,
                        latency=0.1,
                        faults=0,
                    )
                ],
                provider_usage={},
            )

    evaluator.runtime_host = FakeRuntimeHost()

    evaluation = evaluator.evaluate_runtime("dummy-runtime", partition="train", seeds=(0,), use_cache=False, use_reference_scales=False)
    public_task = captured["task_runs"][0][0]

    assert public_task.task_id == "oracle.scoring.train.0"
    assert public_task.private_expected is None
    assert public_task.expected == {"oracle_claim_ids": ["claim.goal_outcome"]}
    assert evaluation.run_results[0].verifier_score == 1.0
    assert evaluation.objective_scores["sbar:e2e"] > 0.0


def test_oracle_stage4_parent_and_child_batches_use_same_task_ids(tmp_path: Path) -> None:
    package = _single_exact_oracle_package()
    suite_task = package.task_sets[0].tasks[0].benchmark_task.model_copy(update={"task_id": "suite.train.different"})
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.suite = SimpleNamespace(
        all_tasks=lambda _partition: [suite_task],
        train=[suite_task],
        proxy=[],
        by_id=lambda task_id: suite_task,
        representative_family_tasks=lambda family, partition="train", limit=4: [suite_task],
    )
    evaluator.oracle_package = package
    evaluator.stage4_minibatch_size = 1
    evaluator.epsilon_full = 0.0
    evaluator.delta_rej = 1.0
    evaluator.reference_profile = SimpleNamespace(evaluation=SimpleNamespace(full_train_seeds=(0,)))
    evaluator.evidence_ledger_path = tmp_path / "evidence.jsonl"
    evaluator.paired_comparison_ledger_path = tmp_path / "paired.jsonl"
    evaluator.promotion_ledger_path = tmp_path / "promotion.jsonl"
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    calls: list[tuple[str, list[str]]] = []

    def fake_evaluate_runtime(runtime_dir, partition="train", seeds=(0,), use_cache=True, tasks_override=None, **kwargs):
        tasks = evaluator._resolve_evaluation_tasks(partition, tasks_override, allow_train_fallback=True)
        calls.append((str(runtime_dir), [task.task_id for task in tasks]))
        score = 0.0 if str(runtime_dir) == "parent" else 1.0
        return scorer.suite_score(
            str(runtime_dir),
            {task.task_id: task.family for task in tasks},
            [
                RunResult(
                    runtime_hash=str(runtime_dir),
                    task_id=task.task_id,
                    seed=0,
                    artifact="sealed-answer",
                    verifier_score=score,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                )
                for task in tasks
            ],
            task_metadata={task.task_id: dict(task.metadata) for task in tasks},
            evaluation_identity={"runtime_spec_digest": str(runtime_dir)},
        )

    evaluator.evaluate_runtime = fake_evaluate_runtime
    evaluator._load_runtime = lambda runtime_dir: SimpleNamespace(runtime_hash=str(runtime_dir))
    evaluator._score_calculator = lambda use_reference_scales=True: scorer
    evaluator._stage4_decision = lambda parent_eval, child_eval: PromotionDecision(
        decision_id="decision",
        decision_type="no_progress",
        contract_id=package.evidence_contract.contract_id,
        parent_runtime_hash=parent_eval.runtime_hash,
        child_runtime_hash=child_eval.runtime_hash,
        quality_delta_lower=1.0,
        quality_delta_estimate=1.0,
    )
    evaluator._write_stage4_ledgers = lambda parent_eval, child_eval, decision: decision

    evaluator.stage4_full(Path("parent"), Path("child"))

    parent_task_ids = calls[0][1]
    child_task_ids = calls[1][1]
    assert set(parent_task_ids) == set(child_task_ids) == {"oracle.scoring.train.0"}
    assert "suite.train.different" not in set(parent_task_ids) | set(child_task_ids)


def test_provider_override_takes_precedence_for_agent_nodes() -> None:
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.provider")
    nodes = [
        spec.graph.nodes[0].model_copy(update={"node_type": "agent"}, deep=True),
        spec.graph.nodes[1],
    ]
    spec = spec.model_copy(update={"graph": spec.graph.model_copy(update={"nodes": nodes}, deep=True)}, deep=True)

    class Provider:
        def __init__(self, text: str) -> None:
            self.text = text
            self.calls = 0

        def generate(self, _request):
            self.calls += 1
            return SimpleNamespace(text=self.text)

    default_provider = Provider("default")
    override_provider = Provider("override")
    state = compile_runtime_spec(spec, provider=default_provider, provider_override=override_provider).invoke("prompt")

    assert state.artifacts["answer"] == "override"
    assert default_provider.calls == 0
    assert override_provider.calls == 1


def test_runtime_langgraph_solve_time_modules_do_not_import_factory_or_oracle() -> None:
    root = Path(__file__).resolve().parents[1] / "agintor" / "runtime" / "langgraph"
    solve_time = {"adapters.py", "entrypoint.py", "executor.py", "operation_service.py", "state.py"}
    forbidden = {"agintor.factory", "agintor.search", "agintor.evaluation", "agintor.oracle"}
    for name in solve_time:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        assert not any(
            imported == blocked or imported.startswith(f"{blocked}.")
            for imported in imports
            for blocked in forbidden
        )


def test_spec_mutator_writes_typed_child_runtime_and_ledger(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "parent"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.parent"),
        runtime_dir,
        force=True,
    )
    (runtime_dir / "oracle").mkdir()
    (runtime_dir / "oracle" / "public.json").write_text("{}", encoding="utf-8")

    candidate = HeuristicSpecActionMutator().mutate(
        SpecMutationContext(
            objective="sbar:global",
            touched_scope=["top"],
            runtime_dir=runtime_dir,
            workspace=tmp_path / "work",
            oracle_package_hash="oracle.hash",
        )
    )
    child = load_runtime(candidate.child_runtime_dir, runtime_profile=load_runtime_profile(), runtime_backend="local")
    ledger_path = candidate.child_runtime_dir / "mutation_ledger.jsonl"
    ledger_rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    action_ref = child.runtime_spec.mutation_history[-1]

    assert candidate.actions
    assert candidate.child_spec_digest != candidate.parent_spec_digest
    assert candidate.child_spec_digest == child.runtime_spec.spec_digest
    assert action_ref.child_spec_digest == child.runtime_spec.spec_digest
    assert ledger_path.is_file()
    assert ledger_rows[0]["result"]["child_spec_digest"] == child.runtime_spec.spec_digest
    assert (candidate.child_runtime_dir / "oracle" / "public.json").is_file()
    assert child.manifest.oracle_package_hash == "oracle.hash"


def test_provider_spec_mutator_rejects_local_provider_for_spec_runtime(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "spec-runtime"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.provider-spec-local"),
        runtime_dir,
        force=True,
    )

    with pytest.raises(ValueError, match="hosted provider"):
        EvolutionEngine(
            load_suite("demo"),
            tmp_path / "evolution",
            LocalDeterministicProvider(),
            runtime_dir,
            mutator_type="provider-spec",
            runtime_backend="local",
            runtime_profile=load_runtime_profile(),
            artifact_mode="none",
        )


def test_oracle_package_rejected_for_policy_module_runtime(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "policy-runtime")
    package = OracleCompiler().compile(
        _goal(),
        baseline_langgraph_runtime_spec(runtime_id="runtime.oracle-policy-guard"),
    )
    package_dir = tmp_path / "oracle"
    write_oracle_package(package, package_dir)

    with pytest.raises(ValueError, match="spec-backed runtime"):
        EvolutionEngine(
            load_suite("demo"),
            tmp_path / "evolution",
            LocalDeterministicProvider(),
            runtime_dir,
            oracle_package=package_dir,
            runtime_backend="local",
            runtime_profile=load_runtime_profile(runtime_dir),
            artifact_mode="none",
        )


def test_tradingagents_compiler_emits_current_runtime_spec_shape() -> None:
    spec = tradingagents_spec_from_goal(_goal())

    assert spec.runtime_kind == "tradingagents_langgraph"
    assert spec.graph.entry_node == "node.market"
    assert spec.graph.terminal_nodes == ["node.terminal"]
    assert spec.spec_digest


def test_tradingagents_compile_load_and_run_returns_terminal_artifact(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "tradingagents-runtime"
    spec = tradingagents_spec_from_goal(_goal())
    RuntimeSpecCompiler().compile_to_directory(spec, runtime_dir, force=True)
    loaded = load_runtime(runtime_dir, runtime_profile=load_runtime_profile(), runtime_backend="local")

    assert loaded.runtime_spec.runtime_kind == "tradingagents_langgraph"
    assert loaded.runtime_spec.selected_analysts == spec.selected_analysts

    class RiskProvider(LocalDeterministicProvider):
        def generate(self, request):
            response = super().generate(request)
            return response.model_copy(update={"text": "RISK_OK"})

    payload = run_spec_task(
        runtime_dir,
        BenchmarkTask(
            task_id="trading.terminal-artifact",
            family="e2e",
            prompt="Evaluate SPY under the bounded trading policy.",
            task_type="structured_ops",
            expected="RISK_OK",
            verifier_type="exact",
        ),
        request_id="req.trading.terminal",
        provider=RiskProvider(),
    )

    assert payload["artifact"] == "RISK_OK"


def test_runtime_spec_compiler_rejects_non_empty_destination_without_force(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    stale = runtime_dir / "stale.txt"
    stale.write_text("old runtime content", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        RuntimeSpecCompiler().compile_to_directory(
            baseline_langgraph_runtime_spec(runtime_id="runtime.nonempty"),
            runtime_dir,
            force=False,
        )

    assert stale.read_text(encoding="utf-8") == "old runtime content"


def test_compiled_spec_manifest_lists_runtime_spec_only_as_mutable(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.identity"),
        runtime_dir,
        force=True,
    )
    manifest_payload = json.loads((runtime_dir / "runtime_manifest.json").read_text(encoding="utf-8"))
    identity = runtime_identity_inputs(runtime_dir, runtime_profile=load_runtime_profile())

    assert "runtime_spec.json" in manifest_payload["mutable_files"]
    assert "runtime_spec.json" not in manifest_payload["immutable_manifest"]
    assert "runtime_spec.json" in identity["mutable_files"]
    assert "runtime_spec.json" not in identity["immutable_files"]


def test_seed_runtime_uses_tradingagents_spec_for_runtime_kind(tmp_path: Path) -> None:
    from agintor.factory.export import _write_seed_runtime

    profile = load_runtime_profile()
    runtime_plan = SimpleNamespace(
        plan_id="runtime.trade",
        runtime_kind="tradingagents_langgraph",
        runtime_profile=profile.model_dump(mode="json"),
        oracle_package_hash="",
        oracle_public_ref="",
        oracle_public_view_hash="",
    )

    _write_seed_runtime(tmp_path / "seed", runtime_plan, goal_spec=_goal())
    loaded = load_runtime(tmp_path / "seed", runtime_profile=profile, runtime_backend="local")

    assert loaded.manifest.runtime_kind == "tradingagents_langgraph"
    assert loaded.runtime_spec.runtime_kind == "tradingagents_langgraph"
    assert loaded.manifest.runtime_spec_digest == loaded.runtime_spec.spec_digest
