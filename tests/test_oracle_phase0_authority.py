from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agintor.contracts import (
    BenchmarkTask,
    ClaimGraph,
    ClaimSpec,
    DomainEvidenceContract,
    GoalSpec,
    ObjectiveKind,
    ObjectiveSpec,
    OraclePackage,
    OracleTask,
    OracleTaskSet,
    RunResult,
    ScoringProjection,
    SuiteEvaluation,
    TaskScore,
    ValidationIntent,
    ValidatorSpec,
    baseline_langgraph_runtime_spec,
    freeze_oracle_package,
)
from agintor.evaluation.benchmarks import load_suite
from agintor.evaluation.evaluator import RuntimeEvaluator
from agintor.evaluation.oracle_runner import OracleEvaluationRunner
from agintor.evaluation.progress_oracle import ProgressOracle, ProgressOracleConfig
from agintor.factory.export import _export_candidate_records, _score_rows_for_candidates
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.package_io import load_oracle_package, write_oracle_package
from agintor.oracle.projections import public_oracle_projection
from agintor.oracle.qa import OracleQARunner
from agintor.providers import LocalDeterministicProvider
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.profile import load_runtime_profile
from agintor.search.archive import QualityDiversityArchive, ScopeScheduler, objective_specs_from_oracle_package
from agintor.search.engine import EvolutionEngine


def _contract(axis_id: str | list[str] = "claim.goal_outcome", *, minimum_authority: str = "A4") -> DomainEvidenceContract:
    axis_ids = [axis_id] if isinstance(axis_id, str) else list(axis_id)
    return DomainEvidenceContract(
        contract_id="oracle-contract.phase0",
        domain_kind="validation_backed_runtime",
        version="oracle.v1",
        scope={"domain": "validation_backed_runtime", "axis_ids": axis_ids},
        challenge_distribution={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"], "minimum_frontier_tasks": 1},
        answer_mechanism={"type": "oracle_package"},
        quality_axes=[
            {
                "axis_id": item,
                "promotion_kind": "capability",
                "comparator_type": "hidden_challenge",
                "minimum_authority": minimum_authority,
            }
            for item in axis_ids
        ],
        health_floors={"oracle_package_qa": "pass", "leakage": "pass"},
        leakage_policy={"status_required": True},
    )


def _exact_package(*, private_expected: object | None = "sealed-answer") -> OraclePackage:
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
        inputs={"requires_private_expected": True},
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    task = BenchmarkTask(
        task_id="oracle.phase0.train.0",
        family="e2e",
        prompt="Return the public answer.",
        task_type="oracle_public_task",
        expected=None,
        private_expected=private_expected,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"], "expected_digest": "digest"},
    )
    return OraclePackage(
        package_id="oracle-package.phase0",
        goal_id="goal.phase0",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.phase0.train",
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
        evidence_contract=_contract(claim.claim_id),
        scoring_projection=ScoringProjection(claim_weights={claim.claim_id: 1.0}, hard_claim_ids=[claim.claim_id]),
    )


def _two_claim_two_task_package() -> OraclePackage:
    package = _exact_package()
    first_task = package.task_sets[0].tasks[0]
    first_claim = package.claim_graph.claims[0]
    first_validator = package.validator_specs[0]
    other_claim = ClaimSpec(
        claim_id="claim.other_outcome",
        text="Runtime returns the other sealed answer.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )
    other_validator = first_validator.model_copy(
        update={"validator_id": "validator.exact.other", "claim_ids": [other_claim.claim_id]},
        deep=True,
    )
    other_benchmark = first_task.benchmark_task.model_copy(
        update={"task_id": "oracle.phase0.other.train.0", "private_expected": "other-answer"},
        deep=True,
    )
    other_task = OracleTask(
        task_id=other_benchmark.task_id,
        benchmark_task=other_benchmark,
        claim_ids=[other_claim.claim_id],
        validator_ids=[other_validator.validator_id],
        partition="train",
    )

    return freeze_oracle_package(
        package.model_copy(
            update={
                "claim_graph": ClaimGraph(claims=[first_claim, other_claim]),
                "validator_specs": [first_validator, other_validator],
                "task_sets": [
                    OracleTaskSet(
                        task_set_id="oracle-taskset.phase0.train",
                        partition="train",
                        tasks=[first_task, other_task],
                    )
                ],
                "evidence_contract": _contract([first_claim.claim_id, other_claim.claim_id]),
                "scoring_projection": ScoringProjection(
                    claim_weights={first_claim.claim_id: 1.0, other_claim.claim_id: 1.0},
                    hard_claim_ids=[first_claim.claim_id, other_claim.claim_id],
                ),
            },
            deep=True,
        )
    )


def _exact_and_schema_package(*, schema_inputs: dict[str, object] | None = None) -> OraclePackage:
    exact_claim = ClaimSpec(
        claim_id="claim.answer_exact",
        text="Runtime returns the sealed exact answer.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )
    schema_claim = ClaimSpec(
        claim_id="claim.artifact_schema",
        text="Runtime artifact satisfies the explicit object schema.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )
    exact_validator = ValidatorSpec(
        validator_id="validator.exact",
        family_id="exact_private_answer",
        claim_ids=[exact_claim.claim_id],
        inputs={"requires_private_expected": True},
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    schema_validator = ValidatorSpec(
        validator_id="validator.schema",
        family_id="schema_artifact",
        claim_ids=[schema_claim.claim_id],
        inputs=dict(schema_inputs or {}),
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    task = BenchmarkTask(
        task_id="oracle.phase0.mixed.train.0",
        family="e2e",
        prompt="Return the sealed answer.",
        task_type="oracle_public_task",
        expected=None,
        private_expected="sealed-answer",
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"], "expected_digest": "digest"},
    )
    return OraclePackage(
        package_id="oracle-package.phase0-mixed",
        goal_id="goal.phase0-mixed",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[exact_claim, schema_claim]),
        validator_specs=[exact_validator, schema_validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.phase0-mixed.train",
                partition="train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[exact_claim.claim_id, schema_claim.claim_id],
                        validator_ids=[exact_validator.validator_id, schema_validator.validator_id],
                        partition="train",
                    )
                ],
            )
        ],
        evidence_contract=_contract([exact_claim.claim_id, schema_claim.claim_id]),
        scoring_projection=ScoringProjection(
            claim_weights={exact_claim.claim_id: 1.0, schema_claim.claim_id: 1.0},
            hard_claim_ids=[exact_claim.claim_id, schema_claim.claim_id],
        ),
    )


def _schema_only_package() -> OraclePackage:
    claim = ClaimSpec(
        claim_id="claim.artifact_schema",
        text="Runtime artifact satisfies the explicit schema.",
        claim_type="outcome",
        criticality="hard",
        weight=1.0,
        minimum_authority="A4",
    )
    validator = ValidatorSpec(
        validator_id="validator.schema",
        family_id="schema_artifact",
        claim_ids=[claim.claim_id],
        inputs={"schema": {"type": "object", "required": ["answer"]}},
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    task = BenchmarkTask(
        task_id="oracle.phase0.schema.train.0",
        family="e2e",
        prompt="Return an object with answer.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    return OraclePackage(
        package_id="oracle-package.phase0-schema",
        goal_id="goal.phase0-schema",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[validator],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.phase0-schema.train",
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
        evidence_contract=_contract(claim.claim_id),
        scoring_projection=ScoringProjection(claim_weights={claim.claim_id: 1.0}, hard_claim_ids=[claim.claim_id]),
    )


def test_exact_private_answer_survives_write_load_only_in_sealed_context(tmp_path: Path) -> None:
    written = write_oracle_package(_exact_package(), tmp_path / "oracle")
    loaded = load_oracle_package(tmp_path / "oracle")
    public_payload = public_oracle_projection(loaded)
    public_text = json.dumps(public_payload, sort_keys=True)

    assert loaded.package_hash == written.package_hash
    assert loaded.sealed_payload_digest == written.sealed_payload_digest
    assert loaded.task_sets[0].tasks[0].benchmark_task.private_expected == "sealed-answer"
    assert "sealed-answer" not in public_text
    assert "private_expected" not in public_text
    assert "sealed_payload_digest" not in public_payload
    assert "sealed_payload_digest" not in public_text
    assert all("sealed_payload_digest" not in task for task_set in public_payload["task_sets"] for task in task_set["tasks"])

    validator_results, claim_results = OracleEvaluationRunner().evaluate_run(
        loaded,
        {"task_id": "oracle.phase0.train.0", "artifact": "sealed-answer", "runtime_hash": "runtime"},
    )

    assert validator_results[0].family_id == "exact_private_answer"
    assert validator_results[0].status == "pass"
    assert claim_results[0].satisfied is True
    assert OracleQARunner().run(loaded).passed


def test_load_rejects_tampered_task_sealed_payload_digest(tmp_path: Path) -> None:
    write_oracle_package(_exact_package(), tmp_path / "oracle")
    sealed_path = tmp_path / "oracle" / "sealed.json"
    sealed_payload = json.loads(sealed_path.read_text(encoding="utf-8"))
    sealed_payload["task_sets"][0]["tasks"][0]["sealed_payload_digest"] = "tampered-task-digest"
    sealed_path.write_text(json.dumps(sealed_payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="sealed_payload_digest"):
        load_oracle_package(tmp_path / "oracle")


def test_exact_private_validator_is_not_compiled_without_exact_sealed_answer() -> None:
    goal = GoalSpec(goal_id="goal.no-exact", raw_prompt="build a repo patch assistant", normalized_goal="build a repo patch assistant")
    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="runtime.no-exact"))

    assert all(validator.family_id != "exact_private_answer" for validator in package.validator_specs)
    assert package.metadata["selected_families"]


def test_compiler_skips_schema_validator_without_concrete_artifact_schema() -> None:
    goal = GoalSpec(
        goal_id="goal.no-schema",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
    )

    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="runtime.no-schema"))
    goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")

    assert all(validator.family_id != "schema_artifact" for validator in package.validator_specs)
    assert goal_claim.criticality == "diagnostic"
    assert goal_claim.unverifiable_reason == "missing_concrete_outcome_validator"
    assert OracleQARunner().run(package).passed


def test_compiler_does_not_grant_outcome_authority_for_type_only_schema() -> None:
    goal = GoalSpec(
        goal_id="goal.type-only-schema",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
        constraints={"artifact_schema": {"type": "object"}},
    )

    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="runtime.type-only-schema"))
    goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")
    objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}

    assert all(validator.family_id != "schema_artifact" for validator in package.validator_specs)
    assert goal_claim.criticality == "diagnostic"
    assert goal_claim.unverifiable_reason == "missing_concrete_outcome_validator"
    assert "axis:claim.goal_outcome" not in objective_names
    assert OracleQARunner().run(package).passed


def test_compiler_forces_schema_validator_when_provider_proposes_trace_only() -> None:
    class TraceOnlyProvider:
        def propose_oracle_compiler(self, **_kwargs):
            return {"family_ids": ["trace_state"], "notes": ["try trace only"]}

    goal = GoalSpec(
        goal_id="goal.trace-only-provider",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
        constraints={"artifact_schema": {"required": ["answer"]}},
    )

    package = OracleCompiler(provider=TraceOnlyProvider()).compile(
        goal,
        baseline_langgraph_runtime_spec(runtime_id="runtime.trace-only-provider"),
    )
    family_ids = {validator.family_id for validator in package.validator_specs}
    goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")

    assert "schema_artifact" in family_ids
    assert "schema_artifact" in package.metadata["selected_families"]
    assert any(
        validator.family_id == "schema_artifact" and "claim.goal_outcome" in validator.claim_ids
        for validator in package.validator_specs
    )
    assert goal_claim.criticality == "major"
    assert goal_claim.minimum_authority == "A3"
    assert not goal_claim.unverifiable_reason
    assert OracleQARunner().run(package).passed


def test_compiler_schema_only_provider_still_covers_active_process_claim() -> None:
    class SchemaOnlyProvider:
        def propose_oracle_compiler(self, **_kwargs):
            return {"family_ids": ["schema_artifact"], "notes": ["schema only"]}

    goal = GoalSpec(
        goal_id="goal.schema-only-provider",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
        constraints={"artifact_schema": {"required": ["answer"]}},
    )

    package = OracleCompiler(provider=SchemaOnlyProvider()).compile(
        goal,
        baseline_langgraph_runtime_spec(runtime_id="runtime.schema-only-provider"),
    )
    process_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.process_integrity")
    trace_validator = next(
        (
            validator
            for validator in package.validator_specs
            if validator.family_id == "trace_state" and "claim.process_integrity" in validator.claim_ids
        ),
        None,
    )
    advertised_axes = {axis.axis_id for axis in package.evidence_contract.quality_axes}
    process_tasks = [task for task_set in package.task_sets for task in task_set.tasks if process_claim.claim_id in task.claim_ids]

    assert process_claim.criticality == "major"
    assert process_claim.minimum_authority == "A3"
    assert trace_validator is not None
    assert {"claim.process_integrity", "claim.no_leakage"}.issubset(set(trace_validator.claim_ids))
    assert "claim.process_integrity" in advertised_axes
    assert process_tasks
    assert all(trace_validator.validator_id in task.validator_ids for task in process_tasks)
    assert OracleQARunner().run(package).passed


def test_compiler_uses_explicit_artifact_schema_without_string_default() -> None:
    schema = {"required": ["answer"]}
    normalized_schema = {"required": ["answer"], "type": "object"}
    goal = GoalSpec(
        goal_id="goal.object-schema",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
        constraints={"artifact_schema": schema},
    )

    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="runtime.object-schema"))
    schema_validators = [validator for validator in package.validator_specs if validator.family_id == "schema_artifact"]
    goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")
    leakage_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.no_leakage")

    assert [validator.inputs["schema"] for validator in schema_validators] == [normalized_schema]
    assert goal_claim.criticality == "major"
    assert goal_claim.minimum_authority == "A3"
    assert leakage_claim.criticality == "major"
    assert leakage_claim.minimum_authority == "A3"
    assert OracleQARunner().run(package).passed


def test_compiler_accepts_object_schema_with_required_and_properties() -> None:
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    goal = GoalSpec(
        goal_id="goal.object-schema-properties",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
        constraints={"artifact_schema": schema},
    )

    package = OracleCompiler().compile(
        goal,
        baseline_langgraph_runtime_spec(runtime_id="runtime.object-schema-properties"),
    )
    schema_validator = next(validator for validator in package.validator_specs if validator.family_id == "schema_artifact")
    goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")

    assert schema_validator.inputs["schema"] == schema
    assert goal_claim.criticality == "major"
    assert goal_claim.minimum_authority == "A3"
    assert not goal_claim.unverifiable_reason
    assert OracleQARunner().run(package).passed


def test_compiler_does_not_grant_outcome_authority_for_unenforced_schema_shapes() -> None:
    schemas = [
        {"properties": {"answer": {"type": "string"}}},
        {"items": {"type": "string"}},
        {"type": "array", "required": ["answer"]},
    ]

    for index, schema in enumerate(schemas):
        goal = GoalSpec(
            goal_id=f"goal.unenforced-schema-{index}",
            raw_prompt="return a JSON artifact report",
            normalized_goal="return a JSON artifact report",
            constraints={"artifact_schema": schema},
        )

        package = OracleCompiler().compile(
            goal,
            baseline_langgraph_runtime_spec(runtime_id=f"runtime.unenforced-schema-{index}"),
        )
        goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")
        objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}

        assert all(validator.family_id != "schema_artifact" for validator in package.validator_specs)
        assert goal_claim.criticality == "diagnostic"
        assert goal_claim.unverifiable_reason == "missing_concrete_outcome_validator"
        assert "axis:claim.goal_outcome" not in objective_names
        assert OracleQARunner().run(package).passed


def test_compiled_a3_schema_and_trace_claims_receive_a3_credit() -> None:
    goal = GoalSpec(
        goal_id="goal.object-schema-credit",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
        constraints={"artifact_schema": {"required": ["answer"]}},
    )
    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="runtime.object-schema-credit"))
    task_id = package.task_sets[0].tasks[0].task_id
    run = RunResult(
        runtime_hash="runtime",
        task_id=task_id,
        seed=0,
        artifact={"answer": "ok"},
        verifier_score=0.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace=[{"event": "langgraph_node_completed"}],
    )
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={f"s:{task_id}": 0.0},
        task_scores={},
        family_scores={},
        run_results=[run],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    rescored = evaluator._score_oracle_results([run], partition="train", tasks=[package.task_sets[0].tasks[0].public_task()])
    scored = evaluator._with_oracle_objective_scores(evaluation)

    assert rescored[0].verifier_score == 1.0
    assert scored.objective_scores["axis:claim.goal_outcome"] == 1.0
    assert scored.objective_scores["axis:claim.no_leakage"] == 1.0


def test_qa_rejects_exact_private_validator_without_sealed_input() -> None:
    report = OracleQARunner().run(_exact_package(private_expected=None))

    assert not report.passed
    assert "missing_sealed_validator_inputs" in report.reason_codes


def test_qa_accepts_exact_private_contract_satisfied_by_sealed_task() -> None:
    package = _exact_package()
    validator = package.validator_specs[0].model_copy(
        update={"metadata": {"input_contract": {"requires": ["artifact", "private_expected"]}}},
        deep=True,
    )
    package = freeze_oracle_package(package.model_copy(update={"validator_specs": [validator]}, deep=True))

    report = OracleQARunner().run(package)

    assert report.passed
    assert "unsatisfied_validator_input_contracts" not in report.reason_codes


def test_qa_rejects_schema_validator_without_explicit_schema() -> None:
    package = _exact_and_schema_package(schema_inputs={})
    report = OracleQARunner().run(package)
    validator_results, _claim_results = OracleEvaluationRunner().evaluate_run(
        package,
        {"task_id": "oracle.phase0.mixed.train.0", "artifact": "sealed-answer", "runtime_hash": "runtime"},
    )
    schema_result = next(result for result in validator_results if result.family_id == "schema_artifact")

    assert not report.passed
    assert "unsatisfied_validator_input_contracts" in report.reason_codes
    assert "validator_controls_failed" in report.reason_codes
    assert schema_result.status == "abstain"


def test_qa_rejects_controls_below_active_claim_authority_floor() -> None:
    report = OracleQARunner().run(_schema_only_package())
    validator_control = next(check for check in report.checks if check["name"] == "validator_controls")

    assert not report.passed
    assert "validator_controls_failed" in report.reason_codes
    assert "validator.schema" in validator_control["details"]["authority_failures"]
    assert validator_control["details"]["authority_failures"]["validator.schema"][0]["minimum_authority"] == "A4"
    assert validator_control["details"]["authority_failures"]["validator.schema"][0]["actual_authority"] == "A3"


def test_qa_uses_contract_axis_authority_floor_for_active_claim() -> None:
    package = _schema_only_package()
    claims = [
        claim.model_copy(update={"minimum_authority": "A3"}, deep=True)
        if claim.claim_id == "claim.artifact_schema"
        else claim
        for claim in package.claim_graph.claims
    ]
    package = freeze_oracle_package(
        package.model_copy(update={"claim_graph": ClaimGraph(claims=claims)}, deep=True)
    )

    report = OracleQARunner().run(package)
    validator_control = next(check for check in report.checks if check["name"] == "validator_controls")

    assert not report.passed
    assert "validator_controls_failed" in report.reason_codes
    assert validator_control["details"]["authority_failures"]["validator.schema"][0]["minimum_authority"] == "A4"
    assert validator_control["details"]["authority_failures"]["validator.schema"][0]["actual_authority"] == "A3"


def test_repo_patch_validator_ignores_runtime_artifact_evaluator_receipt() -> None:
    claim = ClaimSpec(
        claim_id="claim.repo_patch_correct",
        text="Patch validation depends on evaluator-owned runner evidence.",
        claim_type="state",
        criticality="hard",
        minimum_authority="A4",
    )
    validator = ValidatorSpec(
        validator_id="validator.repo",
        family_id="repo_patch",
        claim_ids=[claim.claim_id],
        inputs={
            "repo_snapshot_digest": "repo",
            "public_test_command_digest": "public",
            "hidden_tests_digest": "hidden",
        },
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    task = BenchmarkTask(
        task_id="oracle.repo-patch.train.0",
        family="e2e",
        prompt="Patch the repo.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    package = freeze_oracle_package(
        OraclePackage(
            package_id="oracle-package.repo-patch",
            goal_id="goal.repo-patch",
            validation_intent=ValidationIntent(),
            claim_graph=ClaimGraph(claims=[claim]),
            validator_specs=[validator],
            task_sets=[
                OracleTaskSet(
                    task_set_id="oracle-taskset.repo-patch.train",
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
            evidence_contract=_contract(claim.claim_id),
        )
    )
    receipt = {
        "runner_digest": "runner",
        "repo_snapshot_digest": "repo",
        "public_test_command_digest": "public",
        "hidden_tests_digest": "hidden",
        "applied": True,
        "public_tests_passed": True,
        "hidden_tests_passed": True,
        "tampered_tests": False,
    }
    stale_receipt = {
        **receipt,
        "repo_snapshot_digest": "other-repo",
        "public_test_command_digest": "other-public",
        "hidden_tests_digest": "other-hidden",
    }

    spoofed_results, spoofed_claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "artifact": {"evaluator_receipt": receipt},
        },
    )
    evaluator_results, evaluator_claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "artifact": {"patch": "runtime-controlled"},
            "repo_patch_result": receipt,
        },
    )
    stale_results, stale_claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task.task_id,
            "runtime_hash": "runtime",
            "artifact": {"patch": "runtime-controlled"},
            "repo_patch_result": stale_receipt,
        },
    )

    assert spoofed_results[0].status == "fail"
    assert spoofed_results[0].authority_used == "A0"
    assert spoofed_claims[0].satisfied is False
    assert stale_results[0].status == "fail"
    assert stale_results[0].authority_used == "A0"
    assert stale_results[0].observations["reason"] == "receipt_fixture_digest_mismatch"
    assert stale_claims[0].satisfied is False
    assert evaluator_results[0].status == "pass"
    assert evaluator_results[0].authority_used == "A4"
    assert evaluator_claims[0].satisfied is True


def test_qa_rejects_vacuous_trace_state_and_stateful_service() -> None:
    claim = ClaimSpec(
        claim_id="claim.process_integrity",
        text="Runtime follows process obligations.",
        claim_type="process",
        criticality="hard",
        minimum_authority="A3",
    )
    task = BenchmarkTask(
        task_id="oracle.vacuous.train.0",
        family="e2e",
        prompt="Do the task.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )

    trace_package = OraclePackage(
        package_id="oracle-package.vacuous-trace",
        goal_id="goal.vacuous",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[
            ValidatorSpec(
                validator_id="validator.trace",
                family_id="trace_state",
                claim_ids=[claim.claim_id],
                inputs={},
                visibility="sealed",
                health_tests=["positive_control", "negative_control"],
            )
        ],
        task_sets=[OracleTaskSet(task_set_id="oracle-taskset.vacuous.train", tasks=[OracleTask(task_id=task.task_id, benchmark_task=task, claim_ids=[claim.claim_id], validator_ids=["validator.trace"])])],
        evidence_contract=_contract(claim.claim_id),
    )
    stateful_package = trace_package.model_copy(
        update={
            "package_id": "oracle-package.vacuous-stateful",
            "validator_specs": [
                ValidatorSpec(
                    validator_id="validator.stateful",
                    family_id="stateful_service",
                    claim_ids=[claim.claim_id],
                    inputs={},
                    visibility="sealed",
                    health_tests=["positive_control", "negative_control"],
                )
            ],
            "task_sets": [
                OracleTaskSet(
                    task_set_id="oracle-taskset.vacuous-stateful.train",
                    tasks=[
                        OracleTask(
                            task_id=task.task_id,
                            benchmark_task=task,
                            claim_ids=[claim.claim_id],
                            validator_ids=["validator.stateful"],
                        )
                    ],
                )
            ],
        },
        deep=True,
    )

    trace_report = OracleQARunner().run(trace_package)
    stateful_report = OracleQARunner().run(stateful_package)

    assert not trace_report.passed
    assert "unsatisfied_validator_input_contracts" in trace_report.reason_codes
    assert not stateful_report.passed
    assert "unsatisfied_validator_input_contracts" in stateful_report.reason_codes


def test_qa_rejects_task_claim_with_missing_task_local_validator_id() -> None:
    claim = ClaimSpec(
        claim_id="claim.process_integrity",
        text="Runtime emits required process telemetry.",
        claim_type="process",
        criticality="hard",
        minimum_authority="A3",
    )
    task = BenchmarkTask(
        task_id="oracle.task-local-validator.train.0",
        family="e2e",
        prompt="Do the task.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    validator = ValidatorSpec(
        validator_id="validator.trace",
        family_id="trace_state",
        claim_ids=[claim.claim_id],
        inputs={"required_events": ["langgraph_node_completed"]},
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    package = freeze_oracle_package(
        OraclePackage(
            package_id="oracle-package.task-local-validator",
            goal_id="goal.task-local-validator",
            validation_intent=ValidationIntent(),
            claim_graph=ClaimGraph(claims=[claim]),
            validator_specs=[validator],
            task_sets=[
                OracleTaskSet(
                    task_set_id="oracle-taskset.task-local-validator.train",
                    tasks=[
                        OracleTask(
                            task_id=task.task_id,
                            benchmark_task=task,
                            claim_ids=[claim.claim_id],
                            validator_ids=["validator.trace.typo"],
                        )
                    ],
                )
            ],
            evidence_contract=_contract(claim.claim_id),
        )
    )

    report = OracleQARunner().run(package)
    coverage = next(check for check in report.checks if check["name"] == "task_validator_coverage")

    assert not report.passed
    assert "missing_task_local_validators" in report.reason_codes
    assert coverage["details"]["missing"] == [{"task_id": task.task_id, "claim_id": claim.claim_id}]
    assert coverage["details"]["unknown_validator_ids"] == [{"task_id": task.task_id, "validator_id": "validator.trace.typo"}]


@pytest.mark.parametrize(
    ("criticality", "unverifiable_reason", "failure_action"),
    [
        ("minor", "", "abstain"),
        ("diagnostic", "manual review required", "reject"),
    ],
)
def test_qa_blocks_unsatisfied_controls_for_active_non_major_objectives(
    criticality: str,
    unverifiable_reason: str,
    failure_action: str,
) -> None:
    claim = ClaimSpec(
        claim_id=f"claim.active_{criticality}",
        text="Active non-major claim still participates in oracle objectives.",
        claim_type="process",
        criticality=criticality,
        minimum_authority="A3",
        unverifiable_reason=unverifiable_reason,
    )
    task = BenchmarkTask(
        task_id=f"oracle.active-{criticality}.train.0",
        family="e2e",
        prompt="Do the task.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    validator = ValidatorSpec(
        validator_id="validator.trace",
        family_id="trace_state",
        claim_ids=[claim.claim_id],
        inputs={},
        visibility="sealed",
        health_tests=["positive_control", "negative_control"],
        failure_action=failure_action,
    )
    package = freeze_oracle_package(
        OraclePackage(
            package_id=f"oracle-package.active-{criticality}",
            goal_id=f"goal.active-{criticality}",
            validation_intent=ValidationIntent(),
            claim_graph=ClaimGraph(claims=[claim]),
            validator_specs=[validator],
            task_sets=[
                OracleTaskSet(
                    task_set_id=f"oracle-taskset.active-{criticality}.train",
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
            evidence_contract=_contract(claim.claim_id),
        )
    )

    objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}
    report = OracleQARunner().run(package)

    assert f"axis:{claim.claim_id}" in objective_names
    assert not report.passed
    assert "unsatisfied_validator_input_contracts" in report.reason_codes
    assert "validator_controls_failed" in report.reason_codes


def test_qa_stateful_controls_generate_negative_states_distinct_from_expected() -> None:
    claim = ClaimSpec(
        claim_id="claim.service_state_correct",
        text="Service reaches the sealed expected state.",
        claim_type="state",
        criticality="hard",
        minimum_authority="A4",
    )
    task = BenchmarkTask(
        task_id="oracle.stateful-collision.train.0",
        family="e2e",
        prompt="Update service state.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    expected_state = {"wrong": True}
    validator = ValidatorSpec(
        validator_id="validator.stateful",
        family_id="stateful_service",
        claim_ids=[claim.claim_id],
        inputs={"expected_state": expected_state},
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    package = freeze_oracle_package(
        OraclePackage(
            package_id="oracle-package.stateful-collision",
            goal_id="goal.stateful-collision",
            validation_intent=ValidationIntent(),
            claim_graph=ClaimGraph(claims=[claim]),
            validator_specs=[validator],
            task_sets=[
                OracleTaskSet(
                    task_set_id="oracle-taskset.stateful-collision.train",
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
            evidence_contract=_contract(claim.claim_id),
        )
    )

    assert OracleQARunner().run(package).passed


def test_qa_accepts_required_only_trace_state_controls() -> None:
    claim = ClaimSpec(
        claim_id="claim.process_integrity",
        text="Runtime emits the required event.",
        claim_type="process",
        criticality="hard",
        minimum_authority="A3",
    )
    task = BenchmarkTask(
        task_id="oracle.trace-required.train.0",
        family="e2e",
        prompt="Do the task.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    package = OraclePackage(
        package_id="oracle-package.trace-required",
        goal_id="goal.trace-required",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[
            ValidatorSpec(
                validator_id="validator.trace",
                family_id="trace_state",
                claim_ids=[claim.claim_id],
                inputs={"required_events": ["langgraph_node_completed"]},
                visibility="sealed",
                health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
            )
        ],
        task_sets=[OracleTaskSet(task_set_id="oracle-taskset.trace-required.train", tasks=[OracleTask(task_id=task.task_id, benchmark_task=task, claim_ids=[claim.claim_id], validator_ids=["validator.trace"])])],
        evidence_contract=_contract(claim.claim_id, minimum_authority="A3"),
    )

    assert OracleQARunner().run(package).passed


def test_forbidden_only_trace_state_requires_trace_presence() -> None:
    claim = ClaimSpec(
        claim_id="claim.no_leakage",
        text="Runtime does not emit sealed leakage events.",
        claim_type="process",
        criticality="hard",
        minimum_authority="A3",
    )
    task = BenchmarkTask(
        task_id="oracle.trace-forbidden.train.0",
        family="e2e",
        prompt="Do the task without leaking.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        metadata={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"]},
    )
    package = OraclePackage(
        package_id="oracle-package.trace-forbidden",
        goal_id="goal.trace-forbidden",
        validation_intent=ValidationIntent(),
        claim_graph=ClaimGraph(claims=[claim]),
        validator_specs=[
            ValidatorSpec(
                validator_id="validator.trace",
                family_id="trace_state",
                claim_ids=[claim.claim_id],
                inputs={"forbidden_events": ["sealed_value_read"]},
                visibility="sealed",
                health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
            )
        ],
        task_sets=[
            OracleTaskSet(
                task_set_id="oracle-taskset.trace-forbidden.train",
                tasks=[
                    OracleTask(
                        task_id=task.task_id,
                        benchmark_task=task,
                        claim_ids=[claim.claim_id],
                        validator_ids=["validator.trace"],
                    )
                ],
            )
        ],
        evidence_contract=_contract(claim.claim_id, minimum_authority="A3"),
    )

    empty_results, empty_claims = OracleEvaluationRunner().evaluate_run(
        package,
        {"task_id": task.task_id, "runtime_hash": "runtime", "trace": []},
    )
    clean_results, clean_claims = OracleEvaluationRunner().evaluate_run(
        package,
        {"task_id": task.task_id, "runtime_hash": "runtime", "trace": [{"event": "langgraph_node_completed"}]},
    )

    assert OracleQARunner().run(package).passed
    assert empty_results[0].status == "fail"
    assert empty_results[0].authority_used == "A0"
    assert empty_claims[0].satisfied is False
    assert clean_results[0].status == "pass"
    assert clean_results[0].authority_used == "A3"
    assert clean_claims[0].satisfied is True


def test_oracle_search_objectives_match_oracle_evaluation_axes(tmp_path: Path) -> None:
    package = write_oracle_package(_exact_package(), tmp_path / "oracle")
    objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}

    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={"s:oracle.phase0.train.0": 1.0},
        task_scores={
            "oracle.phase0.train.0": TaskScore(
                s=1.0,
                rho=1.0,
                cvar=1.0,
                utilities=[1.0],
                verifier_scores=[1.0],
                costs=[0.0],
                latencies=[0.0],
                faults=[0],
            )
        },
        family_scores={"e2e": {"s": 1.0, "rho": 1.0}},
        run_results=[
            RunResult(
                runtime_hash="runtime",
                task_id="oracle.phase0.train.0",
                seed=0,
                artifact="sealed-answer",
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
            )
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    runtime_dir = tmp_path / "runtime"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.phase0-objectives"),
        runtime_dir,
        force=True,
    )
    engine = EvolutionEngine(
        load_suite("demo"),
        tmp_path / "evolution",
        LocalDeterministicProvider(),
        runtime_dir,
        runtime_backend="local",
        runtime_profile=load_runtime_profile(),
        artifact_mode="none",
        oracle_package=tmp_path / "oracle",
    )

    scored = evaluator._with_oracle_objective_scores(evaluation)

    assert objective_names == {"s:oracle.phase0.train.0", "axis:claim.goal_outcome"}
    assert {spec.name for spec in engine.objectives} == objective_names
    assert objective_names.issubset(scored.objective_scores)


def test_oracle_search_objectives_omit_task_scalar_for_multi_claim_task() -> None:
    package = _exact_and_schema_package(schema_inputs={"schema": {"type": "object", "required": ["answer"]}})
    objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}

    assert objective_names == {"axis:claim.answer_exact", "axis:claim.artifact_schema"}


def test_oracle_search_scalar_objectives_require_task_local_validator_coverage() -> None:
    package = _exact_package()
    first = package.task_sets[0].tasks[0]
    second_benchmark = first.benchmark_task.model_copy(update={"task_id": "oracle.phase0.unvalidated.train.1"}, deep=True)
    second = first.model_copy(
        update={
            "task_id": second_benchmark.task_id,
            "benchmark_task": second_benchmark,
            "validator_ids": [],
        },
        deep=True,
    )
    package = freeze_oracle_package(
        package.model_copy(
            update={
                "task_sets": [
                    OracleTaskSet(
                        task_set_id="oracle-taskset.phase0.train",
                        partition="train",
                        tasks=[first, second],
                    )
                ]
            },
            deep=True,
        )
    )

    objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}

    assert f"s:{first.public_task().task_id}" in objective_names
    assert f"s:{second.public_task().task_id}" not in objective_names
    assert "axis:claim.goal_outcome" in objective_names


def test_oracle_search_objectives_exclude_diagnostic_unverifiable_claims() -> None:
    goal = GoalSpec(
        goal_id="goal.diagnostic-objective",
        raw_prompt="return a JSON object report",
        normalized_goal="return a JSON object report",
    )
    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="runtime.diagnostic-objective"))
    goal_claim = next(claim for claim in package.claim_graph.claims if claim.claim_id == "claim.goal_outcome")
    objective_names = {spec.name for spec in objective_specs_from_oracle_package(package)}

    assert goal_claim.criticality == "diagnostic"
    assert goal_claim.unverifiable_reason
    assert "axis:claim.goal_outcome" not in objective_names
    assert all(not name.startswith("s:") for name in objective_names)
    assert {"axis:claim.process_integrity", "axis:claim.no_leakage"}.issubset(objective_names)


def test_oracle_search_objectives_reject_unmapped_contract_axes() -> None:
    package = _exact_package().model_copy(
        update={"evidence_contract": _contract(["claim.goal_outcome", "claim.not_backed_by_task"])},
        deep=True,
    )

    with pytest.raises(ValueError, match="not backed by OracleTask.claim_ids"):
        objective_specs_from_oracle_package(package)


def test_oracle_axis_scores_use_per_claim_results_for_shared_task() -> None:
    package = _exact_and_schema_package(schema_inputs={"schema": {"type": "object", "required": ["answer"]}})
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={"s:oracle.phase0.mixed.train.0": 0.0},
        task_scores={
            "oracle.phase0.mixed.train.0": TaskScore(
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
        family_scores={"e2e": {"s": 0.0, "rho": 0.0}},
        run_results=[
            RunResult(
                runtime_hash="runtime",
                task_id="oracle.phase0.mixed.train.0",
                seed=0,
                artifact="sealed-answer",
                verifier_score=0.0,
                cost=0.0,
                latency=0.0,
                faults=0,
            )
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    scored = evaluator._with_oracle_objective_scores(evaluation)

    assert scored.objective_scores["axis:claim.answer_exact"] == 1.0
    assert scored.objective_scores["axis:claim.artifact_schema"] == 0.0


def test_oracle_axis_scores_enforce_claim_authority_floor() -> None:
    package = _exact_and_schema_package(schema_inputs={"schema": {"type": "object", "required": ["answer"]}})
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={"s:oracle.phase0.mixed.train.0": 0.0},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(
                runtime_hash="runtime",
                task_id="oracle.phase0.mixed.train.0",
                seed=0,
                artifact={"answer": "ok"},
                verifier_score=0.0,
                cost=0.0,
                latency=0.0,
                faults=0,
            )
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package
    validator_results, claim_results = OracleEvaluationRunner().evaluate_run(
        package,
        {"task_id": "oracle.phase0.mixed.train.0", "artifact": {"answer": "ok"}, "runtime_hash": "runtime"},
    )
    schema_result = next(result for result in validator_results if result.family_id == "schema_artifact")
    schema_claim = next(result for result in claim_results if result.claim_id == "claim.artifact_schema")

    scored = evaluator._with_oracle_objective_scores(evaluation)

    assert schema_result.authority_used == "A3"
    assert schema_claim.satisfied is True
    assert scored.objective_scores["axis:claim.artifact_schema"] == 0.0


def test_raw_oracle_task_score_enforces_claim_authority_floor() -> None:
    package = _schema_only_package()
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package
    run = RunResult(
        runtime_hash="runtime",
        task_id="oracle.phase0.schema.train.0",
        seed=0,
        artifact={"answer": "ok"},
        verifier_score=1.0,
        cost=0.0,
        latency=0.0,
        faults=0,
    )

    rescored = evaluator._score_oracle_results([run], partition="train", tasks=[package.task_sets[0].tasks[0].public_task()])

    assert rescored[0].verifier_score == 0.0


def test_hard_invalid_oracle_run_gets_no_axis_fallback_credit() -> None:
    package = _exact_package()
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={"s:oracle.phase0.train.0": 1.0},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(
                runtime_hash="runtime",
                task_id="oracle.phase0.train.0",
                seed=0,
                artifact="sealed-answer",
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                hard_invalid=True,
                invalid_reason="runtime_error",
            )
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    scored = evaluator._with_oracle_objective_scores(evaluation)

    assert scored.objective_scores["axis:claim.goal_outcome"] == 0.0


def test_oracle_axis_projection_filters_nonmatching_runs_before_progress_oracle() -> None:
    package = _two_claim_two_task_package()
    first_task, other_task = package.task_sets[0].tasks
    objective = ObjectiveSpec(name="axis:claim.goal_outcome", kind=ObjectiveKind.GLOBAL, family="oracle")
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={f"s:{first_task.task_id}": 1.0, f"s:{other_task.task_id}": 1.0},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(runtime_hash="runtime", task_id=first_task.task_id, seed=0, artifact="sealed-answer", verifier_score=1.0, cost=0.0, latency=0.0, faults=0),
            RunResult(runtime_hash="runtime", task_id=other_task.task_id, seed=0, artifact="other-answer", verifier_score=1.0, cost=0.0, latency=0.0, faults=0),
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    projected = evaluator._evaluation_for_oracle_objective(evaluation, objective)

    assert [run.task_id for run in projected.run_results] == [first_task.task_id]
    assert projected.run_results[0].verifier_score == 1.0
    assert projected.objective_scores["axis:claim.goal_outcome"] == 1.0


def test_oracle_axis_gate_scores_do_not_fallback_to_unrelated_task_scalars() -> None:
    package = _two_claim_two_task_package()
    _, other_task = package.task_sets[0].tasks
    objective = ObjectiveSpec(name="axis:claim.goal_outcome", kind=ObjectiveKind.GLOBAL, family="oracle")
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={f"s:{other_task.task_id}": 1.0},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(runtime_hash="runtime", task_id=other_task.task_id, seed=0, artifact="other-answer", verifier_score=1.0, cost=0.0, latency=0.0, faults=0)
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    scores = evaluator._objective_score_map_for_gate(evaluation, objective, [other_task.public_task()])

    assert scores == {}


def test_stage4_axis_scores_ignore_claim_results_for_hard_invalid_runs() -> None:
    package = _exact_package()
    task = package.task_sets[0].tasks[0]
    run = RunResult(
        runtime_hash="runtime",
        task_id=task.task_id,
        seed=0,
        artifact="sealed-answer",
        verifier_score=1.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        hard_invalid=True,
        invalid_reason="runtime_error",
    )
    _, claim_results = OracleEvaluationRunner().evaluate_run(
        package,
        {"task_id": task.task_id, "artifact": "sealed-answer", "runtime_hash": "runtime"},
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    axis_scores = evaluator._stage4_axis_scores_for_run(
        run,
        decision=SimpleNamespace(progress_signal=None),
        record_id="record",
        evidence_digest="digest",
        claim_results=claim_results,
    )

    assert any(result.satisfied is True for result in claim_results)
    assert [(score.axis_id, score.score, score.authority) for score in axis_scores] == [(task.task_id, 0.0, "A0")]


def test_oracle_gate_scores_are_task_batch_aware_and_paired() -> None:
    package = _exact_package()
    first = package.task_sets[0].tasks[0]
    second_benchmark = first.benchmark_task.model_copy(update={"task_id": "oracle.phase0.train.1"}, deep=True)
    second = first.model_copy(update={"task_id": second_benchmark.task_id, "benchmark_task": second_benchmark}, deep=True)
    package = freeze_oracle_package(
        package.model_copy(
            update={
                "task_sets": [
                    OracleTaskSet(
                        task_set_id="oracle-taskset.phase0.train",
                        partition="train",
                        tasks=[first, second],
                    )
                ]
            },
            deep=True,
        )
    )
    objective = ObjectiveSpec(name="axis:claim.goal_outcome", kind=ObjectiveKind.GLOBAL, family="oracle")
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores={},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(runtime_hash="parent", task_id=first.task_id, seed=0, artifact="sealed-answer", verifier_score=1.0, cost=0.0, latency=0.0, faults=0),
            RunResult(runtime_hash="parent", task_id=second.task_id, seed=0, artifact="wrong", verifier_score=0.0, cost=0.0, latency=0.0, faults=0),
        ],
    )
    child_eval = SuiteEvaluation(
        runtime_hash="child",
        objective_scores={},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(runtime_hash="child", task_id=second.task_id, seed=0, artifact="sealed-answer", verifier_score=1.0, cost=0.0, latency=0.0, faults=0),
        ],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package

    parent_scores, child_scores = evaluator._objective_score_pairs_for_gate(parent_eval, child_eval, objective, [second.public_task()])

    assert parent_scores == [0.0]
    assert child_scores == [1.0]


def test_stage3_gate_scores_selected_oracle_axis_not_task_scalar() -> None:
    package = _exact_and_schema_package(schema_inputs={"schema": {"type": "object", "required": ["answer"]}})
    task = package.task_sets[0].tasks[0].public_task()
    objective = ObjectiveSpec(name="axis:claim.artifact_schema", kind=ObjectiveKind.GLOBAL, family="oracle")
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores={f"s:{task.task_id}": 0.0, "axis:claim.artifact_schema": 1.0},
        task_scores={},
        family_scores={},
        run_results=[],
    )
    child_eval = SuiteEvaluation(
        runtime_hash="child",
        objective_scores={f"s:{task.task_id}": 1.0, "axis:claim.artifact_schema": 0.0},
        task_scores={},
        family_scores={},
        run_results=[],
    )
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package
    evaluator.epsilon_part = 0.0
    evaluator.reference_profile = SimpleNamespace(evaluation=SimpleNamespace(subset_seeds=(0,)))
    evaluator._objective_subset = lambda _objective: [task]
    evaluator.evaluate_runtime = lambda runtime_dir, **_kwargs: parent_eval if str(runtime_dir) == "parent" else child_eval

    result = evaluator.stage3_local_subset(Path("parent"), Path("child"), objective, epsilon_part=0.0)

    assert not result.passed
    assert result.metrics["delta"] < 0.0


def test_stage4_scalar_oracle_objective_projects_to_single_claim_task() -> None:
    package = _two_claim_two_task_package()
    first_task, other_task = package.task_sets[0].tasks
    objective = ObjectiveSpec(
        name=f"s:{first_task.public_task().task_id}",
        kind=ObjectiveKind.SINGLE_TASK,
        task_id=first_task.public_task().task_id,
        family="e2e",
    )

    def evaluation(runtime_hash: str, first_artifact: object, other_artifact: object) -> SuiteEvaluation:
        return SuiteEvaluation(
            runtime_hash=runtime_hash,
            objective_scores={},
            task_scores={},
            family_scores={},
            run_results=[
                RunResult(runtime_hash=runtime_hash, task_id=first_task.task_id, seed=0, artifact=first_artifact, verifier_score=0.0, cost=0.0, latency=0.0, faults=0),
                RunResult(runtime_hash=runtime_hash, task_id=other_task.task_id, seed=0, artifact=other_artifact, verifier_score=0.0, cost=0.0, latency=0.0, faults=0),
            ],
        )

    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package
    evaluator.evidence_contract = package.evidence_contract
    evaluator.progress_oracle = ProgressOracle(ProgressOracleConfig(confidence_z=0.0, min_quality_comparisons=1))
    evaluator.oracle_qa_report = OracleQARunner().run(package)
    parent_eval = evaluation("parent", "wrong", "other-answer")
    child_eval = evaluation("child", "sealed-answer", "wrong")

    projected_child = evaluator._evaluation_for_oracle_objective(child_eval, objective)
    decision = evaluator._stage4_decision(parent_eval, child_eval, objective=objective)
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert [run.task_id for run in projected_child.run_results] == [first_task.task_id]
    assert projected_child.run_results[0].verifier_score == 1.0
    assert set(comparison.axis_deltas) == {"claim.goal_outcome"}
    assert set(comparison.challenge_ids) == {first_task.task_id}


def test_stage4_decision_and_evidence_use_selected_oracle_axis() -> None:
    package = _exact_and_schema_package(schema_inputs={"schema": {"type": "object", "required": ["answer"]}})
    claims = [
        claim.model_copy(update={"criticality": "major", "minimum_authority": "A3"}, deep=True)
        if claim.claim_id == "claim.artifact_schema"
        else claim
        for claim in package.claim_graph.claims
    ]
    axes = [
        axis.model_copy(update={"minimum_authority": "A3"}, deep=True)
        if getattr(axis, "axis_id", "") == "claim.artifact_schema"
        else axis
        for axis in package.evidence_contract.quality_axes
    ]
    package = freeze_oracle_package(
        package.model_copy(
            update={
                "claim_graph": ClaimGraph(claims=claims),
                "evidence_contract": package.evidence_contract.model_copy(update={"quality_axes": axes}, deep=True),
            },
            deep=True,
        )
    )
    task_id = "oracle.phase0.mixed.train.0"
    exact_objective = ObjectiveSpec(name="axis:claim.answer_exact", kind=ObjectiveKind.GLOBAL, family="oracle")

    def evaluation(runtime_hash: str, artifact: object, verifier_score: float) -> SuiteEvaluation:
        return SuiteEvaluation(
            runtime_hash=runtime_hash,
            objective_scores={f"s:{task_id}": verifier_score},
            task_scores={},
            family_scores={},
            run_results=[
                RunResult(runtime_hash=runtime_hash, task_id=task_id, seed=seed, artifact=artifact, verifier_score=verifier_score, cost=0.0, latency=0.0, faults=0)
                for seed in (0, 1)
            ],
        )

    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.oracle_package = package
    evaluator.evidence_contract = package.evidence_contract
    evaluator.progress_oracle = ProgressOracle(ProgressOracleConfig(confidence_z=0.0, min_quality_comparisons=1))
    evaluator.oracle_qa_report = OracleQARunner().run(package)
    parent_eval = evaluator._with_oracle_objective_scores(evaluation("parent", {"answer": "ok"}, 0.0))
    child_eval = evaluator._with_oracle_objective_scores(evaluation("child", "sealed-answer", 1.0))

    decision = evaluator._stage4_decision(parent_eval, child_eval, objective=exact_objective)
    rows = evaluator._stage4_evidence_rows(child_eval, role="child", decision=decision)
    axis_scores = {score["axis_id"]: score["score"] for score in rows[0]["axis_scores"]}
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert str(decision.decision_type) == "capability"
    assert set(comparison.axis_deltas) == {"claim.answer_exact"}
    assert comparison.axis_deltas["claim.answer_exact"].estimate == 1.0
    assert axis_scores["claim.answer_exact"] == 1.0
    assert axis_scores["claim.artifact_schema"] == 0.0


def test_factory_export_uses_oracle_objectives_when_goal_keys_are_suite_shaped() -> None:
    package = _exact_package()
    objective_names = [spec.name for spec in objective_specs_from_oracle_package(package)]
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={objective_names[0]: 1.0, objective_names[1]: 1.0},
        task_scores={
            "oracle.phase0.train.0": TaskScore(
                s=1.0,
                rho=1.0,
                cvar=1.0,
                utilities=[1.0],
                verifier_scores=[1.0],
                costs=[0.0],
                latencies=[0.0],
                faults=[0],
            )
        },
        family_scores={"e2e": {"s": 1.0, "rho": 1.0}},
        run_results=[
            RunResult(
                runtime_hash="runtime",
                task_id="oracle.phase0.train.0",
                seed=0,
                artifact="sealed-answer",
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
            )
        ],
    )
    archive = QualityDiversityArchive()
    archive.insert(
        "runtime-dir",
        "runtime",
        "code",
        1,
        evaluation,
        scope=[],
        archive_kind="capability",
        objectives=objective_names,
        oracle_package_hash=package.package_hash,
    )
    engine = SimpleNamespace(
        archive=archive,
        oracle_package=package,
        _active_objective_ids=lambda: objective_names,
    )

    candidates = _export_candidate_records(engine, ["s:goal.legacy-suite-key"])

    assert [record.entry.runtime_hash for record in candidates] == ["runtime"]
    assert candidates[0].objective in objective_names


def test_factory_export_validation_scores_all_active_oracle_objectives() -> None:
    package = _two_claim_two_task_package()
    objective_names = [spec.name for spec in objective_specs_from_oracle_package(package)]
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={objective_name: 1.0 for objective_name in objective_names},
        task_scores={},
        family_scores={},
        run_results=[],
    )
    archive = QualityDiversityArchive()
    archive.insert(
        "runtime-dir",
        "runtime",
        "code",
        1,
        evaluation,
        scope=[],
        archive_kind="capability",
        objectives=objective_names,
        oracle_package_hash=package.package_hash,
    )
    validation_scores = {
        objective_name: 1.0 if index == 0 else 0.0
        for index, objective_name in enumerate(objective_names)
    }
    calls: list[str] = []

    class FakeEngine:
        oracle_package = package

        def __init__(self) -> None:
            self.archive = archive

        def _active_objective_ids(self) -> list[str]:
            return objective_names

        def evaluate_validation_for_objective(self, _runtime_dir: Path, objective_name: str) -> SuiteEvaluation:
            calls.append(objective_name)
            return SuiteEvaluation(
                runtime_hash="runtime",
                objective_scores={objective_name: validation_scores[objective_name]},
                task_scores={},
                family_scores={},
                run_results=[],
            )

    engine = FakeEngine()
    candidates = _export_candidate_records(engine, ["s:goal.legacy-suite-key"])
    rows = _score_rows_for_candidates(engine, candidates, ["s:goal.legacy-suite-key"])

    assert calls == objective_names
    assert rows[0]["validation_score"] == 0.0
    assert rows[0]["validation_evaluated"] is True
    assert rows[0]["export_eligible"] is False


def test_factory_export_continues_after_top_oracle_candidate_fails_validation() -> None:
    package = _two_claim_two_task_package()
    objective_names = [spec.name for spec in objective_specs_from_oracle_package(package)]

    def evaluation(runtime_hash: str, score: float) -> SuiteEvaluation:
        return SuiteEvaluation(
            runtime_hash=runtime_hash,
            objective_scores={objective_name: score for objective_name in objective_names},
            task_scores={},
            family_scores={},
            run_results=[],
        )

    archive = QualityDiversityArchive()
    archive.insert(
        "runtime-dir-top",
        "runtime-top",
        "code-top",
        1,
        evaluation("runtime-top", 1.0),
        scope=[],
        archive_kind="capability",
        objectives=objective_names,
        oracle_package_hash=package.package_hash,
    )
    archive.insert(
        "runtime-dir-lower",
        "runtime-lower",
        "code-lower",
        1,
        evaluation("runtime-lower", 0.8),
        scope=["top"],
        archive_kind="capability",
        objectives=objective_names,
        oracle_package_hash=package.package_hash,
    )
    validation_by_runtime = {"runtime-dir-top": 0.0, "runtime-dir-lower": 1.0}
    calls: list[tuple[str, str]] = []

    class FakeEngine:
        oracle_package = package

        def __init__(self) -> None:
            self.archive = archive

        def _active_objective_ids(self) -> list[str]:
            return objective_names

        def evaluate_validation_for_objective(self, runtime_dir: Path, objective_name: str) -> SuiteEvaluation:
            runtime_dir_text = str(runtime_dir)
            calls.append((runtime_dir_text, objective_name))
            return SuiteEvaluation(
                runtime_hash=runtime_dir_text,
                objective_scores={objective_name: validation_by_runtime[runtime_dir_text]},
                task_scores={},
                family_scores={},
                run_results=[],
            )

    engine = FakeEngine()
    candidates = _export_candidate_records(engine, ["s:goal.legacy-suite-key"])
    rows = _score_rows_for_candidates(engine, candidates, ["s:goal.legacy-suite-key"])
    rows_by_hash = {row["runtime_hash"]: row for row in rows}

    assert {runtime_dir for runtime_dir, _objective_name in calls} == {"runtime-dir-top", "runtime-dir-lower"}
    assert [objective_name for runtime_dir, objective_name in calls if runtime_dir == "runtime-dir-top"] == objective_names
    assert [objective_name for runtime_dir, objective_name in calls if runtime_dir == "runtime-dir-lower"] == objective_names
    assert rows_by_hash["runtime-top"]["validation_score"] == 0.0
    assert rows_by_hash["runtime-top"]["export_eligible"] is False
    assert rows_by_hash["runtime-lower"]["validation_score"] == 1.0
    assert rows_by_hash["runtime-lower"]["export_eligible"] is True


def test_compiler_skips_not_yet_wired_phase0_families_for_common_goals() -> None:
    unsupported = {"factual_grounded", "consent_proof", "human_audit"}
    prompts = [
        "research current facts and cite sources",
        "ask for authorization before any consent gated side effect",
        "require signed human audit review",
    ]

    for index, prompt in enumerate(prompts):
        package = OracleCompiler().compile(
            GoalSpec(goal_id=f"goal.unsupported.{index}", raw_prompt=prompt, normalized_goal=prompt),
            baseline_langgraph_runtime_spec(runtime_id=f"runtime.unsupported.{index}"),
        )
        families = {validator.family_id for validator in package.validator_specs}

        assert families.isdisjoint(unsupported)
        assert OracleQARunner().run(package).passed


def test_oracle_validation_tick_scores_active_oracle_objective() -> None:
    package = _two_claim_two_task_package()
    objectives = objective_specs_from_oracle_package(package)
    objective_names = [spec.name for spec in objectives]
    score_by_objective = {objective_name: 0.9 - (index * 0.1) for index, objective_name in enumerate(objective_names)}
    called: list[dict[str, object]] = []

    class FakeEvaluator:
        def _objective_subset(self, objective):
            called.append({"objective": objective.name, "tasks_override": [objective.name]})
            return [objective.name]

        def evaluate_runtime(self, runtime_dir, partition="train", seeds=(0,), tasks_override=None, **_kwargs):
            objective_name = list(tasks_override or [None])[0]
            called[-1]["partition"] = partition
            called[-1]["seeds"] = tuple(seeds)
            return SuiteEvaluation(
                runtime_hash="runtime",
                objective_scores={str(objective_name): score_by_objective[str(objective_name)]},
                task_scores={},
                family_scores={},
                run_results=[],
            )

        def evaluate_validation(self, _runtime_dir):
            raise AssertionError("oracle validation tick should not use normal val partition")

    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.oracle_package = package
    engine.objectives = objectives
    engine.objective_ids = set(objective_names)
    engine.evaluator = FakeEvaluator()
    engine.runtime_profile = SimpleNamespace(evaluation=SimpleNamespace(validation_seeds=(7,)))
    engine._progress_island = lambda objective_name: [
        SimpleNamespace(
            runtime_dir="runtime-dir",
            entry=SimpleNamespace(runtime_hash="runtime", scores={objective_name: 0.5}),
        )
    ]
    engine.validation_history = []
    engine.best_val_score = float("-inf")
    engine.history = []
    engine.scheduler = ScopeScheduler()

    engine._validation_tick(5)

    assert [entry["objective"] for entry in called] == objective_names
    assert all(entry["partition"] == "train" and entry["seeds"] == (7,) for entry in called)
    assert engine.validation_history[0]["validation_scores_by_objective"] == score_by_objective
    assert engine.validation_history[0]["validation_score"] == min(score_by_objective.values())
    assert engine.best_val_score == min(score_by_objective.values())
