from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from agintor.contracts import (
    BenchmarkTask,
    ClaimGraph,
    ClaimSpec,
    DomainEvidenceContract,
    EvidenceScope,
    GraphNodeSpec,
    GraphSpec,
    OraclePackage,
    OracleTask,
    OracleTaskSet,
    RunManifest,
    RunResult,
    RuntimeEvidenceManifest,
    RuntimeSpec,
    RuntimeToolSpec,
    SolveRequest,
    ValidationIntent,
    ValidatorSpec,
    baseline_langgraph_runtime_spec,
    freeze_oracle_package,
)
from agintor.evaluation.oracle_runner import OracleEvaluationRunner
from agintor.evaluation.runners.repo_patch_runner import (
    RepoPatchCommand,
    RepoPatchEvaluatorRunner,
    RepoPatchFixture,
    command_suite_digest,
    environment_digest,
    repo_snapshot_digest,
)
from agintor.oracle.qa import OracleQARunner
from agintor.runtime.api.results import solve_result_from_run_result_with_context
from agintor.runtime.host.finalization import FinalizationMixin
from agintor.runtime.langgraph.adapters import run_spec_task
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.langgraph.operation_service import RuntimeOperationService
from agintor.runtime.langgraph.state import LangGraphRuntimeState, build_runtime_evidence_manifest


def _write_fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_public.py").write_text("PUBLIC_TEST = True\n", encoding="utf-8")


def _command(name: str, expression: str) -> RepoPatchCommand:
    return RepoPatchCommand(
        name=name,
        command=[
            sys.executable,
            "-c",
            f"from pathlib import Path\ntext = Path('src/app.py').read_text(encoding='utf-8')\nassert {expression}, text\n",
        ],
        timeout_s=10.0,
    )


def _fixture(repo: Path) -> tuple[RepoPatchFixture, dict[str, Any]]:
    public = _command("public", "'VALUE = 2' in text")
    hidden = _command("hidden", "text.strip() == 'VALUE = 2'")
    fixture = RepoPatchFixture(
        repo_snapshot_path=str(repo),
        public_test_commands=[public],
        sealed_test_commands=[hidden],
        protected_paths=["tests"],
    )
    inputs = {
        "repo_snapshot_path": str(repo),
        "public_test_commands": [public.model_dump(mode="json")],
        "sealed_test_commands": [hidden.model_dump(mode="json")],
        "protected_paths": ["tests"],
        "repo_snapshot_digest": repo_snapshot_digest(repo),
        "public_test_command_digest": command_suite_digest([public]),
        "hidden_tests_digest": command_suite_digest([hidden]),
        "qa_known_good_artifact": _patch_artifact(),
    }
    return fixture, inputs


def _public_only_fixture(repo: Path) -> tuple[RepoPatchFixture, dict[str, Any]]:
    public = _command("public", "'VALUE = 2' in text")
    fixture = RepoPatchFixture(
        repo_snapshot_path=str(repo),
        public_test_commands=[public],
        sealed_test_commands=[],
        protected_paths=["tests"],
    )
    inputs = {
        "repo_snapshot_path": str(repo),
        "public_test_commands": [public.model_dump(mode="json")],
        "sealed_test_commands": [],
        "protected_paths": ["tests"],
        "repo_snapshot_digest": repo_snapshot_digest(repo),
        "public_test_command_digest": command_suite_digest([public]),
        "qa_known_good_artifact": _patch_artifact(),
    }
    return fixture, inputs


def _manifest(task_id: str) -> dict[str, Any]:
    return RuntimeEvidenceManifest(
        request_id="req.repo-proof",
        task_id=task_id,
        runtime_hash="runtime",
        runtime_spec_digest="runtime-spec",
        trace_events=[{"event": "langgraph_node_completed", "node_id": "patch", "node_type": "repo_patch"}],
        side_effect_receipts=[
            {
                "side_effect_id": "repo-patch.intent",
                "action_kind": "filesystem_write",
                "node_id": "patch",
                "status": "completed",
            }
        ],
    ).model_dump(mode="json", exclude_none=True)


def _package(inputs: dict[str, Any] | None = None, *, family_id: str = "repo_patch") -> OraclePackage:
    claim = ClaimSpec(
        claim_id="claim.repo_patch_correct",
        text="Patch applies in an evaluator-controlled copy and passes public and hidden tests.",
        claim_type="state",
        criticality="hard",
        minimum_authority="A4",
    )
    validator = ValidatorSpec(
        validator_id=f"validator.{family_id}",
        family_id=family_id,
        claim_ids=[claim.claim_id],
        inputs=inputs or {},
        visibility="sealed",
        health_tests=["positive_control", "negative_control", "empty_artifact", "irrelevant_artifact", "leakage_canary"],
        failure_action="reject",
    )
    task = BenchmarkTask(
        task_id=f"oracle.{family_id}.train.0",
        family="e2e",
        prompt="Patch the repo.",
        task_type="oracle_public_task",
        expected=None,
        verifier_type="oracle_package",
        verification_required=True,
    )
    return freeze_oracle_package(
        OraclePackage(
            package_id=f"oracle-package.{family_id}",
            goal_id=f"goal.{family_id}",
            validation_intent=ValidationIntent(),
            claim_graph=ClaimGraph(claims=[claim]),
            validator_specs=[validator],
            task_sets=[
                OracleTaskSet(
                    task_set_id=f"oracle-taskset.{family_id}.train",
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
            evidence_contract=DomainEvidenceContract(
                contract_id=f"contract.{family_id}",
                domain_kind="repo_patch",
                version="v1",
                scope=EvidenceScope(domain="repo_patch", axis_ids=[claim.claim_id]),
                quality_axes=[
                    {
                        "axis_id": claim.claim_id,
                        "description": claim.text,
                        "minimum_authority": "A4",
                    }
                ],
            ),
        )
    )


def _patch_artifact() -> dict[str, Any]:
    return {"files": [{"path": "src/app.py", "updated_content": "VALUE = 2\n"}]}


def _unified_diff_patch() -> str:
    return "\n".join(
        [
            "diff --git a/src/app.py b/src/app.py",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -1 +1 @@",
            "-VALUE = 1",
            "+VALUE = 2",
            "",
        ]
    )


def test_repo_patch_runner_applies_patch_in_clean_copy_and_keeps_snapshot_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    fixture, _inputs = _fixture(repo)
    original_digest = repo_snapshot_digest(repo)

    result = RepoPatchEvaluatorRunner().run(candidate_artifact=_patch_artifact(), fixture=fixture)

    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is True
    assert result.tampered_tests is False
    assert repo_snapshot_digest(repo) == original_digest
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_repo_patch_runner_treats_absent_public_phase_as_neutral_for_sealed_only_fixture(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    hidden = _command("hidden", "text.strip() == 'VALUE = 2'")
    fixture = RepoPatchFixture(
        repo_snapshot_path=str(repo),
        public_test_commands=[],
        sealed_test_commands=[hidden],
        protected_paths=["tests"],
    )

    result = RepoPatchEvaluatorRunner().run(candidate_artifact=_patch_artifact(), fixture=fixture)

    assert result.status == "pass"
    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is True
    assert result.public_command_results == []
    assert len(result.hidden_command_results) == 1


def test_repo_patch_fixture_from_spec_inputs_applies_fixture_timeout_to_string_and_list_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    timeout_s = 1.25

    fixture = RepoPatchFixture.from_spec_inputs(
        {
            "repo_snapshot_path": str(repo),
            "public_test_commands": [f"{sys.executable} -c \"pass\""],
            "sealed_test_commands": [[sys.executable, "-c", "pass"]],
            "timeout_s": timeout_s,
        }
    )

    assert fixture is not None
    assert fixture.timeout_s == timeout_s
    assert fixture.public_test_commands[0].timeout_s == timeout_s
    assert fixture.sealed_test_commands[0].timeout_s == timeout_s


def test_repo_patch_validator_captures_public_hidden_command_log_environment_and_fixture_digests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": _patch_artifact(),
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    result = results[0]
    assert result.status == "pass"
    assert result.authority_used == "A4"
    assert claims[0].satisfied is True
    assert result.observations["command_digests"] == {
        "public": inputs["public_test_command_digest"],
        "hidden": inputs["hidden_tests_digest"],
    }
    assert result.observations["log_digests"]
    assert result.observations["environment_digest"]
    assert result.observations["fixture_digest"]


def test_repo_patch_validator_replaces_presupplied_receipt_when_fixture_available(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id
    stale_receipt = {
        "runner_digest": "stale-runner",
        "repo_snapshot_digest": inputs["repo_snapshot_digest"],
        "public_test_command_digest": inputs["public_test_command_digest"],
        "hidden_tests_digest": inputs["hidden_tests_digest"],
        "applied": True,
        "public_tests_passed": True,
        "hidden_tests_passed": True,
        "tampered_tests": False,
    }

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"files": [{"path": "src/app.py", "updated_content": "VALUE = 1\n"}]},
            "repo_patch_result": stale_receipt,
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    assert results[0].status == "fail"
    assert results[0].observations["runner_digest"] != "stale-runner"
    assert results[0].observations["public_tests_passed"] is False
    assert claims[0].satisfied is False


def test_repo_patch_validator_preserves_raw_unified_diff_artifacts_for_runner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": _unified_diff_patch(),
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    assert results[0].status == "pass"
    assert results[0].authority_used == "A4"
    assert claims[0].satisfied is True


def test_repo_patch_runner_normalizes_runtime_updated_files_absolute_diff_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    fixture, _inputs = _fixture(repo)
    absolute_path = (repo / "src" / "app.py").resolve()
    artifact = {
        "updated_files": [
            {
                "path": str(absolute_path),
                "diff": "\n".join(
                    [
                        f"--- {absolute_path}",
                        f"+++ {absolute_path}",
                        "@@ -1 +1 @@",
                        "-VALUE = 1",
                        "+VALUE = 2",
                        "",
                    ]
                ),
            }
        ]
    }

    result = RepoPatchEvaluatorRunner().run(candidate_artifact=artifact, fixture=fixture)

    assert result.status == "pass"
    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is True


def test_repo_patch_public_only_fixture_passes_at_artifact_authority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _public_only_fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": _patch_artifact(),
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    assert results[0].status == "pass"
    assert results[0].authority_used == "A3"
    assert results[0].observations["hidden_tests_passed"] is None
    assert claims[0].satisfied is True


def test_repo_patch_artifact_flag_spoofing_without_runner_evidence_fails() -> None:
    package = _package(
        {
            "repo_snapshot_digest": "repo",
            "public_test_command_digest": "public",
            "hidden_tests_digest": "hidden",
        }
    )
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"applied": True, "public_tests_passed": True, "hidden_tests_passed": True},
        },
    )

    assert results[0].status == "fail"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "artifact_flag_spoof_without_runner_evidence"
    assert claims[0].satisfied is False


def test_repo_patch_receipt_booleans_must_be_strict_bools() -> None:
    package = _package(
        {
            "repo_snapshot_digest": "repo",
            "public_test_command_digest": "public",
            "hidden_tests_digest": "hidden",
        }
    )
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"patch": "receipt-only"},
            "repo_patch_result": {
                "runner_digest": "runner",
                "repo_snapshot_digest": "repo",
                "public_test_command_digest": "public",
                "hidden_tests_digest": "hidden",
                "applied": "false",
                "public_tests_passed": "false",
                "hidden_tests_passed": "false",
                "tampered_tests": "false",
            },
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    assert results[0].status == "fail"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "malformed_repo_patch_receipt_booleans"
    assert results[0].observations["malformed_fields"] == [
        "applied",
        "public_tests_passed",
        "tampered_tests",
        "hidden_tests_passed",
    ]
    assert claims[0].satisfied is False


def test_repo_patch_sealed_receipt_requires_hidden_digest_for_private_authority() -> None:
    package = _package(
        {
            "repo_snapshot_digest": "repo",
            "public_test_command_digest": "public",
        }
    )
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"patch": "receipt-only"},
            "repo_patch_result": {
                "runner_digest": "runner",
                "repo_snapshot_digest": "repo",
                "public_test_command_digest": "public",
                "applied": True,
                "public_tests_passed": True,
                "hidden_tests_passed": True,
                "tampered_tests": False,
            },
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    assert results[0].status == "fail"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "missing_hidden_tests_digest_for_sealed_evidence"
    assert results[0].observations["missing_digests"] == [
        "spec.hidden_tests_digest",
        "receipt.hidden_tests_digest",
    ]
    assert claims[0].satisfied is False


def test_repo_patch_test_tampering_quarantines_runner_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {
                "files": [
                    {"path": "src/app.py", "updated_content": "VALUE = 2\n"},
                    {"path": "tests/test_public.py", "updated_content": "PUBLIC_TEST = False\n"},
                ]
            },
            "runtime_evidence_manifest": _manifest(task_id),
        },
    )

    assert results[0].status == "quarantine"
    assert results[0].observations["tampered_tests"] is True
    assert results[0].observations["tampered_paths"] == ["tests"]
    assert claims[0].satisfied is False


def test_repo_patch_foreign_manifest_cannot_support_runner_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id
    foreign_manifest = RuntimeEvidenceManifest(
        request_id="req.repo-foreign",
        task_id="oracle.foreign.train.0",
        runtime_hash="foreign-runtime",
        runtime_spec_digest="runtime-spec",
        trace_events=[{"event": "langgraph_node_completed", "node_id": "patch", "node_type": "repo_patch"}],
    ).model_dump(mode="json", exclude_none=True)

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": _patch_artifact(),
            "runtime_evidence_manifest": foreign_manifest,
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "runtime_evidence_manifest_identity_mismatch"
    assert results[0].observations["mismatched_fields"] == ["task_id", "runtime_hash"]
    assert claims[0].satisfied is None


def test_repo_patch_request_mismatched_manifest_cannot_support_runner_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id
    stale_manifest = RuntimeEvidenceManifest(
        request_id="req.repo-stale",
        task_id=task_id,
        runtime_hash="runtime",
        runtime_spec_digest="runtime-spec",
        trace_events=[{"event": "langgraph_node_completed", "node_id": "patch", "node_type": "repo_patch"}],
    ).model_dump(mode="json", exclude_none=True)

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "request_id": "req.repo-current",
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": _patch_artifact(),
            "runtime_evidence_manifest": stale_manifest,
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "runtime_evidence_manifest_identity_mismatch"
    assert results[0].observations["mismatched_fields"] == ["request_id"]
    assert claims[0].satisfied is None


def test_repo_patch_empty_manifest_without_typed_repo_patch_evidence_abstains(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)
    task_id = package.task_sets[0].tasks[0].task_id
    empty_manifest = RuntimeEvidenceManifest(
        request_id="req.repo-empty",
        task_id=task_id,
        runtime_hash="runtime",
        runtime_spec_digest="runtime-spec",
    ).model_dump(mode="json", exclude_none=True)

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": _patch_artifact(),
            "runtime_evidence_manifest": empty_manifest,
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "missing_repo_patch_runtime_evidence"
    assert claims[0].satisfied is None


def test_manifest_required_validators_abstain_without_runtime_evidence_manifest() -> None:
    package = _package(
        {
            "required_events": ["langgraph_node_completed"],
        },
        family_id="trace_state",
    )
    task_id = package.task_sets[0].tasks[0].task_id

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"answer": "ok"},
            "trace": [{"event": "langgraph_node_completed"}],
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "missing_runtime_evidence_manifest"
    assert claims[0].satisfied is None


def test_trace_state_rejects_foreign_runtime_evidence_manifest_identity() -> None:
    package = _package(
        {
            "required_events": ["langgraph_node_completed"],
        },
        family_id="trace_state",
    )
    task_id = package.task_sets[0].tasks[0].task_id
    foreign_manifest = RuntimeEvidenceManifest(
        request_id="req.foreign",
        task_id="oracle.foreign.train.0",
        runtime_hash="foreign-runtime",
        runtime_spec_digest="runtime-spec",
        trace_events=[{"event": "langgraph_node_completed"}],
    ).model_dump(mode="json", exclude_none=True)

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"answer": "ok"},
            "runtime_evidence_manifest": foreign_manifest,
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "runtime_evidence_manifest_identity_mismatch"
    assert results[0].observations["mismatched_fields"] == ["task_id", "runtime_hash"]
    assert claims[0].satisfied is None


def test_trace_state_rejects_request_mismatched_runtime_evidence_manifest_identity() -> None:
    package = _package(
        {
            "required_events": ["langgraph_node_completed"],
        },
        family_id="trace_state",
    )
    task_id = package.task_sets[0].tasks[0].task_id
    stale_manifest = RuntimeEvidenceManifest(
        request_id="req.trace-stale",
        task_id=task_id,
        runtime_hash="runtime",
        runtime_spec_digest="runtime-spec",
        trace_events=[{"event": "langgraph_node_completed"}],
    ).model_dump(mode="json", exclude_none=True)

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "request_id": "req.trace-current",
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"answer": "ok"},
            "runtime_evidence_manifest": stale_manifest,
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "runtime_evidence_manifest_identity_mismatch"
    assert results[0].observations["mismatched_fields"] == ["request_id"]
    assert claims[0].satisfied is None


def test_trace_state_rejects_manifest_that_omits_forbidden_captured_trace_event() -> None:
    package = _package(
        {
            "forbidden_events": ["sealed_value_read"],
        },
        family_id="trace_state",
    )
    task_id = package.task_sets[0].tasks[0].task_id
    manifest = RuntimeEvidenceManifest(
        request_id="req.trace-divergence",
        task_id=task_id,
        runtime_hash="runtime",
        runtime_spec_digest="runtime-spec",
        trace_events=[{"event": "langgraph_node_completed"}],
    ).model_dump(mode="json", exclude_none=True)

    results, claims = OracleEvaluationRunner().evaluate_run(
        package,
        {
            "request_id": "req.trace-divergence",
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"answer": "ok"},
            "trace": [{"event": "langgraph_node_completed"}, {"event": "sealed_value_read"}],
            "runtime_evidence_manifest": manifest,
        },
    )

    assert results[0].status == "abstain"
    assert results[0].authority_used == "A0"
    assert results[0].observations["reason"] == "runtime_evidence_manifest_trace_divergence"
    assert results[0].observations["missing_from_manifest"] == ["sealed_value_read"]
    assert claims[0].satisfied is None


def test_repo_patch_qa_controls_use_manifest_for_artifact_level_failures() -> None:
    package = _package(
        {
            "repo_snapshot_digest": "repo",
            "public_test_command_digest": "public",
            "hidden_tests_digest": "hidden",
        }
    )

    report = OracleQARunner().run(package)

    assert report.passed


def test_repo_patch_qa_fixture_controls_run_known_good_artifact_in_clean_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    package = _package(inputs)

    report = OracleQARunner().run(package)

    assert report.passed


def test_repo_patch_qa_requires_hidden_digest_when_sealed_commands_are_declared(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    _fixture_obj, inputs = _fixture(repo)
    inputs.pop("hidden_tests_digest")
    package = _package(inputs)

    report = OracleQARunner().run(package)
    contract_check = next(check for check in report.checks if check["name"] == "validator_input_contracts")

    assert not report.passed
    assert "unsatisfied_validator_input_contracts" in report.reason_codes
    assert "hidden_tests_digest" in contract_check["details"]["blocked"]["validator.repo_patch"]


def test_runtime_evidence_manifest_recomputes_runtime_supplied_digest_fields() -> None:
    package = _package({"required_events": ["langgraph_node_completed"]}, family_id="trace_state")
    task_id = package.task_sets[0].tasks[0].task_id
    payload = _manifest(task_id)
    payload.update(
        {
            "manifest_id": "runtime-forged-manifest",
            "trace_digest": "runtime-forged-trace",
            "side_effect_receipt_digest": "runtime-forged-receipts",
            "evidence_digest": "runtime-forged-evidence",
        }
    )

    validated = RuntimeEvidenceManifest.model_validate(payload)
    _results, _claims, ledger = OracleEvaluationRunner().evaluate_run_with_ledger(
        package,
        {
            "task_id": task_id,
            "runtime_hash": "runtime",
            "artifact": {"answer": "ok"},
            "runtime_evidence_manifest": payload,
        },
    )

    assert validated.manifest_id != "runtime-forged-manifest"
    assert validated.trace_digest != "runtime-forged-trace"
    assert validated.side_effect_receipt_digest != "runtime-forged-receipts"
    assert validated.evidence_digest != "runtime-forged-evidence"
    assert ledger.claim_manifest_digest == validated.evidence_digest


def test_repo_patch_runner_controlled_environment_blocks_unhashed_ambient_env(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _write_fixture(repo)
    command = RepoPatchCommand(
        name="public-env",
        command=[
            sys.executable,
            "-c",
            (
                "import os\n"
                "from pathlib import Path\n"
                "assert os.environ.get('AGINTOR_AMBIENT_REPO_PATCH') is None\n"
                "assert 'VALUE = 2' in Path('src/app.py').read_text(encoding='utf-8')\n"
            ),
        ],
        timeout_s=10.0,
    )
    fixture = RepoPatchFixture(
        repo_snapshot_path=str(repo),
        public_test_commands=[command],
        sealed_test_commands=[],
        protected_paths=["tests"],
    )
    before_digest = environment_digest(fixture)
    monkeypatch.setenv("AGINTOR_AMBIENT_REPO_PATCH", "would-fail-if-inherited")
    after_digest = environment_digest(fixture)

    result = RepoPatchEvaluatorRunner().run(candidate_artifact=_patch_artifact(), fixture=fixture)

    assert result.status == "pass"
    assert result.environment_digest == after_digest == before_digest


def test_langgraph_operation_service_digesting_accepts_non_json_output_and_input(tmp_path: Path) -> None:
    path_node = GraphNodeSpec(
        node_id="path",
        node_type="tool",
        tool_id="path_tool",
        output_key="path",
        static_args={"label": "artifact"},
    )
    merge_node = GraphNodeSpec(
        node_id="merge",
        node_type="merge",
        input_keys=["path"],
        output_key="merged",
    )
    spec = RuntimeSpec(
        runtime_id="runtime.non-json",
        name="Non JSON hashing",
        graph=GraphSpec(entry_node=path_node.node_id, terminal_nodes=[merge_node.node_id], nodes=[path_node, merge_node]),
        tools=[RuntimeToolSpec(tool_id="path_tool", name="Path Tool", family="test", runtime_visible=True)],
    )
    state = LangGraphRuntimeState(
        request_id="req.non-json",
        task_id="task.non-json",
        runtime_hash="runtime.hash",
        runtime_spec_digest=spec.spec_digest,
    )
    service = RuntimeOperationService(spec, tools={"path_tool": lambda **_kwargs: tmp_path / "artifact.txt"})

    first = service.run_node(state, path_node)
    second = service.run_node(state, merge_node)

    assert first.status == "completed"
    assert second.status == "completed"
    assert state.status == "running"
    assert isinstance(state.artifacts["path"], Path)
    completed = [row for row in state.trace if row.get("event") == "langgraph_node_completed"]
    assert len(completed) == 2
    assert all(row.get("output_digest") for row in completed)
    assert completed[1]["input_refs"][0]["digest"]


def test_runtime_evidence_manifest_handles_mixed_key_mappings_in_artifacts_and_trace_metadata(tmp_path: Path) -> None:
    state = LangGraphRuntimeState(
        request_id="req.mixed",
        task_id="task.mixed",
        runtime_hash="runtime.hash",
        runtime_spec_digest="runtime-spec",
        artifacts={"mixed": {1: "one", "two": tmp_path / "two.txt"}},
        trace=[
            {
                "event": "langgraph_node_completed",
                "node_id": "mixed",
                "node_type": "builtin",
                "output_key": "mixed",
                "output_digest": "digest",
                "mixed_metadata": {1: "one", "two": tmp_path / "two.txt"},
            }
        ],
    )

    manifest = build_runtime_evidence_manifest(state)

    assert manifest.artifact_refs[0].key == "mixed"
    assert manifest.artifact_refs[0].digest
    assert manifest.trace_events[0].metadata["mixed_metadata"] == {"1": "one", "two": str(tmp_path / "two.txt")}


def test_spec_backed_runtime_output_includes_minimal_evidence_manifest(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.manifest"),
        runtime_dir,
        force=True,
    )
    task = BenchmarkTask(
        task_id="oracle.manifest.train.0",
        family="e2e",
        prompt="Return ok.",
        task_type="oracle_public_task",
        expected={"oracle_claim_ids": ["claim.goal_outcome"]},
        verifier_type="oracle_package",
    )

    payload = run_spec_task(runtime_dir, task, request_id="req.manifest", runtime_hash="runtime.hash")
    manifest = RuntimeEvidenceManifest.model_validate(payload["runtime_evidence_manifest"])

    assert payload["runtime_hash"] == "runtime.hash"
    assert manifest.request_id == "req.manifest"
    assert manifest.task_id == task.task_id
    assert manifest.runtime_hash == "runtime.hash"
    assert manifest.runtime_spec_digest == payload["runtime_spec_digest"]
    assert [claim.claim_id for claim in manifest.declared_claims] == ["claim.goal_outcome"]
    assert manifest.artifact_refs
    assert manifest.node_io_refs
    assert manifest.trace_digest
    artifact_ref_ids = {ref.ref_id for ref in manifest.artifact_refs}
    artifact_keys = {ref.key for ref in manifest.artifact_refs}
    for node_ref in manifest.node_io_refs:
        if node_ref.output_key in artifact_keys:
            assert node_ref.output_ref_id in artifact_ref_ids


def test_spec_backed_solve_result_roundtrip_preserves_runtime_evidence_manifest(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.roundtrip"),
        runtime_dir,
        force=True,
    )
    task = BenchmarkTask(
        task_id="oracle.manifest-roundtrip.train.0",
        family="e2e",
        prompt="Return ok.",
        task_type="oracle_public_task",
        expected={"oracle_claim_ids": ["claim.goal_outcome"]},
        verifier_type="oracle_package",
    )
    payload = run_spec_task(runtime_dir, task, request_id="req.manifest-roundtrip", runtime_hash="runtime.hash")
    run = RunResult(
        request_id=payload["request_id"],
        task_id=payload["task_id"],
        seed=0,
        runtime_hash=payload["runtime_hash"],
        artifact=payload["artifact"],
        verifier_score=1.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace=payload["trace"],
        runtime_evidence_manifest=payload["runtime_evidence_manifest"],
    )
    solve_result = solve_result_from_run_result_with_context(
        SolveRequest(request_id=payload["request_id"], prompt=task.prompt),
        run,
        payload["runtime_hash"],
        mode="benchmark",
        provider_usage={},
    )
    manifest = RunManifest(
        run_id="run.manifest-roundtrip",
        run_root=str(tmp_path / "run"),
        request_id=payload["request_id"],
        runtime_hash=payload["runtime_hash"],
        runtime_backend="local",
        task_id=payload["task_id"],
        seed=0,
    )

    reconstructed = FinalizationMixin()._run_result_from_solve_result(manifest, solve_result)

    assert solve_result.runtime_evidence_manifest == payload["runtime_evidence_manifest"]
    assert reconstructed.runtime_evidence_manifest == payload["runtime_evidence_manifest"]
