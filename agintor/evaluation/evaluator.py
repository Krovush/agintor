from __future__ import annotations

import ast
import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence
import inspect

from ..storage.artifacts import ArtifactMode, ArtifactPolicy
from ..evaluation.benchmarks import BenchmarkSuite
from ..core.exceptions import PatchApplyError
from ..core.patches import parse_patch
from ..learning.predictors import DecisionFamilyModelBank
from ..factory.prompt_builder import METHOD_CONTRACTS
from ..providers import ModelProvider
from ..runtime.loader import load_runtime
from ..runtime.host import RuntimeHost
from ..runtime.profile import RuntimeProfile, resolve_runtime_profile
from ..evaluation.progress_oracle import ProgressOracle
from ..evaluation.scoring import ScoreCalculator, estimate_reference_scales, mean_improvement
from ..contracts.verifiers import rescore_private_run_results
from ..evaluation.oracle_runner import OracleEvaluationRunner, SealedEvaluatorPayload
from ..oracle.package_io import load_oracle_package
from ..oracle.qa import OracleQARunner
from ..contracts import (
    BenchmarkTask,
    ClaimResult,
    DomainEvidenceContract,
    EvaluationStageResult,
    EvidenceRecord,
    MutationCandidate,
    ObjectiveKind,
    ObjectiveSpec,
    OpenAITraceContext,
    OraclePackage,
    OracleTask,
    OutcomeAxisScore,
    PromotionDecision,
    RunResult,
    SuiteEvaluation,
    ValidatorResult,
    runtime_visible_benchmark_task,
    sealed_benchmark_task_payload,
)
from ..utils import ensure_directory, stable_hash


class RuntimeEvaluator:
    def __init__(
        self,
        suite: BenchmarkSuite,
        workspace: Path,
        provider: ModelProvider,
        baseline_runtime_dir: Path | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
        predictors: DecisionFamilyModelBank | None = None,
        runtime_backend: str | None = None,
        runtime_profile: RuntimeProfile | None = None,
        profile_path: Path | None = None,
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: Path | None = None,
        trace_context: OpenAITraceContext | None = None,
        evidence_contract: DomainEvidenceContract | None = None,
        oracle_package: OraclePackage | str | Path | None = None,
    ) -> None:
        self.suite = suite
        self.workspace = Path(workspace)
        self.provider = provider
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.keep_successes
        self.budget_overrides = dict(budget_overrides or {})
        self.predictors = predictors or DecisionFamilyModelBank()
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self._baseline_runtime_dir = Path(baseline_runtime_dir) if baseline_runtime_dir is not None else None
        self._preparing_reference_scales = False
        self._reference_scales_ready = False
        self.reference_profile = runtime_profile or resolve_runtime_profile(
            baseline_runtime_dir,
            profile_path=self.profile_path,
        )
        self.runtime_backend = (runtime_backend or os.environ.get("AGINTOR_RUNTIME_BACKEND", "local")).strip().lower()
        self.trace_context = trace_context
        self.evidence_contract = evidence_contract or getattr(suite, "evidence_contract", None)
        self.runtime_host = RuntimeHost(
            self.workspace,
            runtime_backend=self.runtime_backend,
            artifact_mode=self.artifact_policy.mode,
            sandbox_root=self.artifact_policy.sandbox_root,
        )
        self.stage1_replays = self.reference_profile.evaluation.stage1_replays
        self.epsilon_proxy = self.reference_profile.evaluation.epsilon_proxy
        self.epsilon_part = self.reference_profile.evaluation.epsilon_part
        self.epsilon_full = self.reference_profile.evaluation.epsilon_full
        self.stage4_minibatch_size = self.reference_profile.evaluation.stage4_minibatch_size
        self.delta_rej = self.reference_profile.evaluation.delta_rej
        self.cache: dict[tuple[str, str, tuple[int, ...], tuple[str, ...], str], SuiteEvaluation] = {}
        self.reference_scales = ({}, {})
        self.last_provider_usage: dict[str, Any] = {}
        self.progress_oracle = ProgressOracle()
        if oracle_package is None or isinstance(oracle_package, OraclePackage):
            self.oracle_package = oracle_package
        else:
            self.oracle_package = load_oracle_package(oracle_package)
        self.oracle_runner = OracleEvaluationRunner()
        self.oracle_qa_runner = OracleQARunner()
        self.oracle_qa_report = None
        if self.oracle_package is not None:
            self.oracle_qa_report = self.oracle_qa_runner.run(self.oracle_package)
            self.evidence_contract = self.oracle_package.evidence_contract
        self.evaluation_workspace = ensure_directory(self.workspace / "evaluation")
        self.evidence_ledger_path = self.evaluation_workspace / "evidence_ledger.jsonl"
        self.paired_comparison_ledger_path = self.evaluation_workspace / "paired_comparisons.jsonl"
        self.promotion_ledger_path = self.evaluation_workspace / "promotion_ledger.jsonl"

    def prepare_reference_scales(self, force: bool = False) -> None:
        if self._baseline_runtime_dir is None:
            return
        if self._reference_scales_ready and not force:
            return
        self._preparing_reference_scales = True
        try:
            kwargs = {
                "partition": "train",
                "seeds": self.reference_profile.evaluation.reference_scale_seeds,
                "use_cache": False,
                "use_reference_scales": False,
            }
            try:
                params = inspect.signature(self.evaluate_runtime).parameters
            except (TypeError, ValueError):
                params = {}
            filtered_kwargs = {key: value for key, value in kwargs.items() if key in params}
            baseline_eval = self.evaluate_runtime(self._baseline_runtime_dir, **filtered_kwargs)
        finally:
            self._preparing_reference_scales = False
        self.reference_scales = estimate_reference_scales(baseline_eval.run_results)
        self._reference_scales_ready = True

    def _score_calculator(self, *, use_reference_scales: bool = True) -> ScoreCalculator:
        if (
            use_reference_scales
            and self._baseline_runtime_dir is not None
            and not self._reference_scales_ready
            and not self._preparing_reference_scales
        ):
            self.prepare_reference_scales()
        costs, latencies = self.reference_scales if use_reference_scales and self._reference_scales_ready else ({}, {})
        return ScoreCalculator(
            baseline_costs=costs,
            baseline_latencies=latencies,
            family_weights=self.reference_profile.evaluation.family_weights,
            lambdas=self.reference_profile.evaluation.lambdas,
            robustness=self.reference_profile.evaluation.robustness,
        )

    def _effective_runtime_profile(self, runtime_dir: str | Path) -> RuntimeProfile:
        return resolve_runtime_profile(
            runtime_dir,
            fallback_profile=self.reference_profile,
            profile_path=self.profile_path,
        )

    def _load_runtime(self, runtime_dir: str | Path, *, runtime_profile: RuntimeProfile | None = None):
        return load_runtime(
            runtime_dir,
            runtime_profile=runtime_profile or self._effective_runtime_profile(runtime_dir),
            runtime_backend=self.runtime_backend,
        )

    def _evaluation_identity(self, runtime: Any) -> dict[str, str]:
        oracle_package = getattr(self, "oracle_package", None)
        runtime_spec_digest = str(
            getattr(getattr(runtime, "manifest", None), "runtime_spec_digest", "")
            or getattr(getattr(runtime, "runtime_spec", None), "spec_digest", "")
            or ""
        )
        oracle_package_hash = str(
            getattr(oracle_package, "package_hash", "")
            or getattr(getattr(runtime, "manifest", None), "oracle_package_hash", "")
            or ""
        )
        return {
            "runtime_kind": str(getattr(getattr(runtime, "manifest", None), "runtime_kind", "") or ""),
            "runtime_spec_digest": runtime_spec_digest,
            "oracle_package_hash": oracle_package_hash,
            "oracle_public_view_hash": str(getattr(oracle_package, "public_view_hash", "") or ""),
            "oracle_sealed_view_hash": str(getattr(oracle_package, "sealed_view_hash", "") or ""),
        }

    def _evaluation_units(self, tasks: Sequence[Any]) -> list[list[Any]]:
        units: list[list[Any]] = []
        episodes: dict[str, list[Any]] = {}
        for task in tasks:
            episode_id = getattr(task, "episode_id", None)
            if getattr(task, "transfer_scored", False) and episode_id:
                if episode_id not in episodes:
                    episodes[episode_id] = []
                    units.append(episodes[episode_id])
                episodes[episode_id].append(task)
                continue
            units.append([task])
        for unit in units:
            if len(unit) > 1 and getattr(unit[0], "episode_id", None):
                unit.sort(key=lambda task: (getattr(task, "episode_order", 0), task.task_id))
        return units

    def _normalize_trace_payload(self, value: Any) -> Any:
        volatile_keys = {
            "dollar_cost",
            "handle_id",
            "latency_s",
            "launch_time",
            "node_id",
            "process_pid",
            "raw_id",
            "stderr_path",
            "stdout_path",
        }
        if isinstance(value, dict):
            return {
                key: self._normalize_trace_payload(item)
                for key, item in sorted(value.items())
                if key not in volatile_keys
            }
        if isinstance(value, list):
            return [self._normalize_trace_payload(item) for item in value]
        return value

    def _normalize_trace(self, trace: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._normalize_trace_payload(event) for event in trace]

    def _trace_rows(self, run) -> list[dict[str, Any]]:
        return run.trace_rows() if hasattr(run, "trace_rows") else []

    def _stage4_ledger_paths(self) -> dict[str, str]:
        return {
            "evidence_ledger_path": str(self.evidence_ledger_path),
            "paired_comparisons_path": str(self.paired_comparison_ledger_path),
            "promotion_ledger_path": str(self.promotion_ledger_path),
        }

    def _append_jsonl(self, path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        ensure_directory(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), sort_keys=True, separators=(",", ":")) + "\n")

    def _run_ref(self, run) -> str:
        return str(getattr(run, "run_root", "") or getattr(run, "run_id", "") or getattr(run, "request_id", ""))

    def _trace_ref(self, run) -> str:
        if getattr(run, "trace_path", None):
            return str(run.trace_path)
        return run.trace_ref() if hasattr(run, "trace_ref") else ""

    def _contract_task_matches(self, task: BenchmarkTask) -> bool:
        if self.evidence_contract is None:
            return False
        metadata = dict(task.metadata or {})
        distribution = dict(self.evidence_contract.challenge_distribution or {})
        expected_domain = str(distribution.get("domain_kind") or self.evidence_contract.domain_kind or "")
        if expected_domain and str(metadata.get("domain_kind", "")) != expected_domain:
            return False
        required_tags = {
            str(tag)
            for tag in distribution.get("slice_tags", [])
            if str(tag)
        }
        task_tags = {str(tag) for tag in metadata.get("slice_tags", [])}
        return required_tags.issubset(task_tags)

    def _stage4_contract_attestation(self, child_eval: SuiteEvaluation) -> tuple[dict[str, str] | None, str | None]:
        if self.evidence_contract is None:
            return None, None
        status: dict[str, str] = {}
        required = {str(key) for key in dict(self.evidence_contract.health_floors or {})}
        run_task_ids = {str(run.task_id) for run in child_eval.run_results}
        suite_tasks = self._authoritative_tasks_for_run_ids(run_task_ids, partition="train")
        suite_tasks = [task for task in suite_tasks if self._contract_task_matches(task)]
        if not suite_tasks and not required and not self.evidence_contract.leakage_policy:
            return {}, None

        metadata_by_id = {task.task_id: dict(task.metadata or {}) for task in suite_tasks}
        distinct_challenges = {task.task_id for task in suite_tasks}
        minimum = int(
            dict(self.evidence_contract.challenge_distribution or {}).get(
                "minimum_frontier_tasks",
                dict(self.evidence_contract.statistical_rule or {}).get("minimum_pairs", 0),
            )
            or 0
        )
        if "generator" in required:
            distribution = dict(self.evidence_contract.challenge_distribution or {})
            expected_generator_id = str(distribution.get("generator_id") or "")
            expected_generator_version = str(distribution.get("generator_version") or "")
            expected_domain_kind = str(distribution.get("domain_kind") or self.evidence_contract.domain_kind or "")
            generator_ok = bool(suite_tasks) and all(
                metadata_by_id[task.task_id].get("generator_id")
                and metadata_by_id[task.task_id].get("generator_version")
                and metadata_by_id[task.task_id].get("domain_kind")
                and (not expected_generator_id or str(metadata_by_id[task.task_id].get("generator_id", "")) == expected_generator_id)
                and (not expected_generator_version or str(metadata_by_id[task.task_id].get("generator_version", "")) == expected_generator_version)
                and (not expected_domain_kind or str(metadata_by_id[task.task_id].get("domain_kind", "")) == expected_domain_kind)
                for task in suite_tasks
            )
            status["generator"] = "pass" if generator_ok else "missing"
        if "answer" in required:
            answer_ok = bool(suite_tasks) and all(
                getattr(task, "private_expected", None) is not None
                for task in suite_tasks
            )
            status["answer"] = "pass" if answer_ok else "missing"
        if "validator" in required or "verifier" in required:
            validator_ok = bool(suite_tasks) and all(
                bool(getattr(task, "verification_required", False))
                and str(getattr(task, "verifier_type", "none")) != "none"
                for task in suite_tasks
            )
            key = "validator" if "validator" in required else "verifier"
            status[key] = "pass" if validator_ok else "missing"
        if "statistics" in required:
            statistics_ok = bool(suite_tasks) and (minimum <= 0 or len(distinct_challenges) >= minimum)
            status["statistics"] = "pass" if statistics_ok else "missing"

        leakage_required = "leakage" in required or bool(self.evidence_contract.leakage_policy)
        leakage_state = "unknown"
        if leakage_required:
            leakage_ok = bool(suite_tasks)
            for task in suite_tasks:
                visible = self._runtime_visible_task(task)
                visible_metadata = dict(visible.metadata or {})
                private_keys_visible = any(
                    str(key).startswith("private_")
                    or str(key) in {"private_answer_ref", "private_answer_mechanism", "private_expected", "expected_digest"}
                    for key in visible_metadata
                )
                if getattr(task, "private_expected", None) is not None:
                    leakage_ok = leakage_ok and visible.expected is None and visible.private_expected is None
                leakage_ok = leakage_ok and not private_keys_visible
            status["leakage"] = "pass" if leakage_ok else ("missing" if not suite_tasks else "fail")
            leakage_state = "clean" if status["leakage"] == "pass" else "unknown"
            if status["leakage"] == "fail":
                leakage_state = "leaked"
        return status, leakage_state

    def _stage4_decision(self, parent_eval: SuiteEvaluation, child_eval: SuiteEvaluation) -> PromotionDecision:
        health_floor_status, leakage_status = self._stage4_contract_attestation(child_eval)
        oracle_package = getattr(self, "oracle_package", None)
        if oracle_package is not None:
            qa_report = getattr(self, "oracle_qa_report", None)
            if qa_report is None:
                qa_runner = getattr(self, "oracle_qa_runner", None) or OracleQARunner()
                qa_report = qa_runner.run(oracle_package)
                self.oracle_qa_report = qa_report
            health_floor_status = dict(health_floor_status or {})
            health_floor_status["oracle_package_qa"] = "pass" if qa_report.passed else "fail"
            health_floor_status["oracle_package_hash"] = oracle_package.package_hash
            health_floor_status["oracle_public_view_hash"] = oracle_package.public_view_hash
        return self.progress_oracle.decide_evaluations(
            parent_eval,
            child_eval,
            contract=self.evidence_contract,
            health_floor_status=health_floor_status,
            leakage_status=leakage_status,
        )

    def _axis_id_for_task(self, decision: PromotionDecision, task_id: str) -> str:
        comparison = decision.progress_signal.pairwise_comparisons[0] if decision.progress_signal and decision.progress_signal.pairwise_comparisons else None
        if comparison is None:
            return task_id
        for axis_id, task_ids in dict(comparison.axis_task_ids or {}).items():
            if str(task_id) in {str(item) for item in task_ids}:
                return str(axis_id)
        return task_id

    def _stage4_evidence_rows(
        self,
        evaluation: SuiteEvaluation,
        *,
        role: str,
        decision: PromotionDecision,
        paired_run_keys: set[tuple[str, int]] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for run in sorted(evaluation.run_results, key=lambda item: (str(item.task_id), int(item.seed), str(item.request_id))):
            if paired_run_keys is not None and (str(run.task_id), int(run.seed)) not in paired_run_keys:
                continue
            checkpoint_ref = str(getattr(run, "latest_checkpoint_ref", None) or getattr(run, "checkpoint_ref", None) or "")
            trace_ref = self._trace_ref(run)
            validator_rows: list[dict[str, Any]] = []
            claim_rows: list[dict[str, Any]] = []
            oracle_package = getattr(self, "oracle_package", None)
            evaluation_identity = dict(getattr(evaluation, "evaluation_identity", {}) or {})
            if oracle_package is not None:
                oracle_task = self._oracle_task_by_runtime_task_id().get(str(run.task_id))
                validator_results, claim_results = self._oracle_evidence_for_run(run, oracle_task=oracle_task)
                validator_rows = [result.model_dump(mode="json", exclude_none=True) for result in validator_results]
                claim_rows = [result.model_dump(mode="json", exclude_none=True) for result in claim_results]
            record_id = stable_hash(
                "stage4.evidence",
                role,
                decision.comparison_ref,
                evaluation.runtime_hash,
                run.task_id,
                int(run.seed),
                self._run_ref(run),
            )[:24]
            digest_payload = {
                "role": role,
                "runtime_hash": evaluation.runtime_hash,
                "task_id": str(run.task_id),
                "seed": int(run.seed),
                "run_ref": self._run_ref(run),
                "trace_ref": trace_ref,
                "checkpoint_ref": checkpoint_ref,
                "artifact": run.artifact,
                "verifier_score": float(run.verifier_score),
                "hard_invalid": bool(run.hard_invalid),
                "invalid_reason": str(run.invalid_reason or ""),
                "oracle_package_hash": str(getattr(oracle_package, "package_hash", "") or ""),
                "runtime_spec_digest": str(evaluation_identity.get("runtime_spec_digest", "") or ""),
                "validator_results": validator_rows,
                "claim_results": claim_rows,
            }
            digest = stable_hash(digest_payload)
            record = EvidenceRecord(
                record_id=record_id,
                contract_id=decision.contract_id,
                challenge_id=str(run.task_id),
                candidate_runtime_hash=evaluation.runtime_hash,
                oracle_package_hash=str(getattr(oracle_package, "package_hash", "") or ""),
                runtime_spec_digest=str(evaluation_identity.get("runtime_spec_digest", "") or ""),
                oracle_public_view_hash=str(getattr(oracle_package, "public_view_hash", "") or ""),
                oracle_sealed_view_hash=str(getattr(oracle_package, "sealed_view_hash", "") or ""),
                validator_results=validator_rows,
                claim_results=claim_rows,
                parent_runtime_hash=decision.parent_runtime_hash,
                run_ref=self._run_ref(run),
                attempt_ref=str(getattr(run, "attempt_id", "") or ""),
                checkpoint_refs=[checkpoint_ref] if checkpoint_ref else [],
                trace_refs=[trace_ref] if trace_ref else [],
                artifact_ref=str(getattr(run, "artifact_ref", "") or ""),
                axis_scores=[
                    OutcomeAxisScore(
                        axis_id=self._axis_id_for_task(decision, str(run.task_id)),
                        score=float(run.verifier_score),
                        authority="A4" if not run.hard_invalid else "A0",
                        evidence_ref=record_id,
                        evidence_digest=digest,
                    )
                ],
                efficiency_scores={
                    "cost": float(run.cost),
                    "latency": float(run.latency),
                    "tokens": float(run.tokens_used or (run.input_tokens + run.output_tokens) or 0),
                    "faults": float(run.faults),
                },
                verifier_evidence=[
                    {
                        "role": role,
                        "verifier_score": float(run.verifier_score),
                        "hard_invalid": bool(run.hard_invalid),
                        "artifact": run.artifact,
                    }
                ],
                authority_level="A4" if not run.hard_invalid else "A0",
                invalid_reason=str(run.invalid_reason or ""),
                evidence_digest=digest,
            )
            rows.append(record.model_dump(mode="json", exclude_none=True))
        return rows

    def _write_stage4_ledgers(self, parent_eval: SuiteEvaluation, child_eval: SuiteEvaluation, decision: PromotionDecision) -> PromotionDecision:
        comparison = decision.progress_signal.pairwise_comparisons[0] if decision.progress_signal and decision.progress_signal.pairwise_comparisons else None
        paired_run_keys = {(str(run.task_id), int(run.seed)) for run in child_eval.run_results}
        if comparison is not None and (comparison.challenge_ids or comparison.axis_task_ids):
            paired_challenges = {str(challenge_id) for challenge_id in comparison.challenge_ids}
            for task_ids in dict(comparison.axis_task_ids or {}).values():
                paired_challenges.update(str(task_id) for task_id in task_ids)
            paired_run_keys = {
                key
                for key in paired_run_keys
                if key[0] in paired_challenges
            }
        parent_rows = self._stage4_evidence_rows(parent_eval, role="parent", decision=decision, paired_run_keys=paired_run_keys)
        child_rows = self._stage4_evidence_rows(child_eval, role="child", decision=decision, paired_run_keys=paired_run_keys)
        evidence_refs = [str(row["record_id"]) for row in [*parent_rows, *child_rows]]
        evidence_digest = stable_hash(
            [
                {"record_id": row["record_id"], "evidence_digest": row.get("evidence_digest", "")}
                for row in [*parent_rows, *child_rows]
            ]
        )
        if comparison is not None:
            decision_id = stable_hash(
                "promotion-decision",
                comparison.comparison_id,
                str(getattr(decision.decision_type, "value", decision.decision_type)),
                list(decision.reason_codes),
                evidence_digest,
            )[:24]
            comparison = comparison.model_copy(
                update={
                    "decision_ref": decision_id,
                    "evidence_refs": evidence_refs,
                    "evidence_digest": evidence_digest,
                }
            )
            if decision.progress_signal is not None:
                signal_id = stable_hash(
                    "progress-signal",
                    comparison.comparison_id,
                    str(getattr(decision.decision_type, "value", decision.decision_type)),
                    evidence_digest,
                )[:24]
                signal = decision.progress_signal.model_copy(
                    update={
                        "signal_id": signal_id,
                        "pairwise_comparisons": [comparison],
                        "evidence_digest": evidence_digest,
                    }
                )
                decision = decision.model_copy(
                    update={
                        "decision_id": decision_id,
                        "progress_signal": signal,
                        "progress_signal_ref": signal_id,
                        "evidence_refs": evidence_refs,
                        "evidence_digest": evidence_digest,
                    }
                )
            else:
                decision = decision.model_copy(
                    update={
                        "decision_id": decision_id,
                        "evidence_refs": evidence_refs,
                        "evidence_digest": evidence_digest,
                    }
                )
        comparison_rows = [comparison.model_dump(mode="json", exclude_none=True)] if comparison is not None else []
        self._append_jsonl(self.evidence_ledger_path, parent_rows + child_rows)
        self._append_jsonl(self.paired_comparison_ledger_path, comparison_rows)
        self._append_jsonl(self.promotion_ledger_path, [decision.model_dump(mode="json", exclude_none=True)])
        return decision

    def _stage4_result_from_decision(
        self,
        decision: PromotionDecision,
        *,
        epsilon_full: float,
        child_eval: SuiteEvaluation,
        reason_prefix: str = "full train progress decision",
    ) -> EvaluationStageResult:
        signal = decision.progress_signal
        decision_type = str(getattr(decision.decision_type, "value", decision.decision_type))
        promoted = decision_type in {"capability", "efficiency", "preference", "subskill"} and not child_eval.invalid
        metrics = {
            "delta": float(decision.quality_delta_estimate or 0.0),
            "lcb": float(decision.quality_delta_lower or 0.0),
            "epsilon_full": epsilon_full,
            "promotion_decision": decision.model_dump(mode="json", exclude_none=True),
            "progress_decision": decision_type,
            "quality_delta_lower": float(decision.quality_delta_lower or 0.0),
            "efficiency_delta_lower": float(decision.efficiency_delta_lower or 0.0),
            "progress_reason_codes": list(decision.reason_codes),
            **self._stage4_ledger_paths(),
        }
        return EvaluationStageResult(
            stage=4,
            passed=promoted,
            reason=f"{reason_prefix}: {decision_type}",
            metrics=metrics,
            suite_evaluation=child_eval,
            progress_signal=signal,
            promotion_decision=decision,
            promotion_type=decision.decision_type,
            promotion_decision_ref=decision.decision_id,
            progress_signal_ref=decision.progress_signal_ref,
            evidence_contract_id=decision.contract_id,
            oracle_package_hash=decision.oracle_package_hash,
            runtime_spec_digest=decision.child_runtime_spec_digest,
        )

    def _cleanup_path(self, path: Path | None, *, failed: bool = False) -> None:
        if path is None or not path.exists():
            return
        if failed and self.artifact_policy.keep_failures:
            return
        if not failed and self.artifact_policy.keep_successes:
            return
        shutil.rmtree(path, ignore_errors=True)

    def _file_contract_snapshot(self, source: str, allowed_methods: Sequence[str]) -> tuple[tuple[str, ...], dict[str, dict[str, str]]]:
        tree = ast.parse(source)
        top_level: list[str] = []
        class_contracts: dict[str, dict[str, str]] = {}
        allowed = set(allowed_methods)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                top_level.append(f"class:{node.name}")
                class_snapshot: dict[str, str] = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name not in allowed:
                            class_snapshot[f"method:{item.name}"] = ast.dump(item, include_attributes=False)
                    else:
                        class_snapshot[f"node:{ast.dump(item, include_attributes=False)}"] = ast.dump(item, include_attributes=False)
                class_contracts[node.name] = class_snapshot
            else:
                top_level.append(ast.dump(node, include_attributes=False))
        return tuple(top_level), class_contracts

    def _ensure_only_allowed_methods_changed(self, parent_source: str, child_source: str, allowed_methods: Sequence[str]) -> None:
        parent_snapshot = self._file_contract_snapshot(parent_source, allowed_methods)
        child_snapshot = self._file_contract_snapshot(child_source, allowed_methods)
        if parent_snapshot != child_snapshot:
            raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")

    def _train_batches(self) -> list[list[Any]]:
        units = self._evaluation_units(self._partition_tasks("train", allow_train_fallback=True))
        batch_size = max(1, self.stage4_minibatch_size)
        batches: list[list[Any]] = []
        current: list[Any] = []
        current_size = 0
        for unit in units:
            unit_size = len(unit)
            if current and current_size + unit_size > batch_size:
                batches.append(current)
                current = []
                current_size = 0
            current.extend(unit)
            current_size += unit_size
            if current_size >= batch_size:
                batches.append(current)
                current = []
                current_size = 0
        if current:
            batches.append(current)
        return batches

    def tighten_thresholds(self, stage_name: str) -> None:
        if stage_name == "stage1":
            self.stage1_replays = min(4, self.stage1_replays + 1)
            return
        if stage_name == "stage2":
            self.epsilon_proxy = max(0.0, self.epsilon_proxy - 0.0025)
            return
        if stage_name == "stage3":
            self.epsilon_part = max(0.0, self.epsilon_part - 0.0025)

    def _runtime_visible_task(self, task: BenchmarkTask) -> BenchmarkTask:
        return runtime_visible_benchmark_task(task)

    def _oracle_task_records(self, partition: str, *, allow_train_fallback: bool = False) -> list[OracleTask]:
        package = getattr(self, "oracle_package", None)
        if package is None:
            return []
        records = [
            task
            for task_set in package.task_sets
            if str(task_set.partition) == str(partition)
            for task in task_set.tasks
        ]
        if records or not allow_train_fallback or str(partition) == "train":
            return records
        return [
            task
            for task_set in package.task_sets
            if str(task_set.partition) == "train"
            for task in task_set.tasks
        ]

    def _oracle_task_by_runtime_task_id(
        self,
        partition: str | None = None,
        *,
        allow_train_fallback: bool = True,
    ) -> dict[str, OracleTask]:
        records: list[OracleTask] = []
        package = getattr(self, "oracle_package", None)
        if package is None:
            return {}
        if partition is None:
            records = [task for task_set in package.task_sets for task in task_set.tasks]
        else:
            records = self._oracle_task_records(partition, allow_train_fallback=allow_train_fallback)
        task_by_id: dict[str, OracleTask] = {}
        for oracle_task in records:
            public_task = oracle_task.public_task()
            for task_id in {oracle_task.task_id, oracle_task.benchmark_task.task_id, public_task.task_id}:
                task_by_id[str(task_id)] = oracle_task
        return task_by_id

    def _partition_tasks(self, partition: str, *, allow_train_fallback: bool = False) -> list[BenchmarkTask]:
        oracle_records = self._oracle_task_records(partition, allow_train_fallback=allow_train_fallback)
        if oracle_records:
            return [record.public_task() for record in oracle_records]
        return list(self.suite.all_tasks(partition))

    def _resolve_evaluation_tasks(
        self,
        partition: str,
        tasks_override: Sequence[Any] | None,
        *,
        allow_train_fallback: bool = False,
    ) -> list[BenchmarkTask]:
        oracle_records = self._oracle_task_records(partition, allow_train_fallback=allow_train_fallback)
        if not oracle_records:
            return list(tasks_override) if tasks_override is not None else list(self.suite.all_tasks(partition))
        if tasks_override is None:
            return [record.public_task() for record in oracle_records]

        override_ids = {
            str(getattr(task, "task_id", "") or "")
            for task in tasks_override
        }
        selected = [
            record
            for record in oracle_records
            if override_ids
            and {
                str(record.task_id),
                str(record.benchmark_task.task_id),
                str(record.public_task().task_id),
            }
            & override_ids
        ]
        if not selected:
            selected = oracle_records
        return [record.public_task() for record in selected]

    def _authoritative_tasks_for_run_ids(self, run_task_ids: set[str], *, partition: str) -> list[BenchmarkTask]:
        oracle_map = self._oracle_task_by_runtime_task_id(partition, allow_train_fallback=True)
        if oracle_map:
            seen: set[str] = set()
            tasks: list[BenchmarkTask] = []
            for task_id, oracle_task in oracle_map.items():
                authoritative_id = str(oracle_task.benchmark_task.task_id)
                if task_id in run_task_ids and authoritative_id not in seen:
                    seen.add(authoritative_id)
                    tasks.append(oracle_task.benchmark_task)
            return tasks
        return [
            task
            for task in getattr(self.suite, partition, [])
            if str(getattr(task, "task_id", "")) in run_task_ids
        ]

    def _oracle_evidence_for_run(
        self,
        run: RunResult,
        *,
        oracle_task: OracleTask | None,
    ) -> tuple[list[ValidatorResult], list[ClaimResult]]:
        oracle_package = getattr(self, "oracle_package", None)
        if oracle_package is None:
            return [], []
        sealed_payload = SealedEvaluatorPayload(
            package=oracle_package,
            trace_events=self._trace_rows(run),
            workspace_root=str(getattr(run, "run_root", "") or ""),
        ).model_dump(mode="json", exclude_none=True)
        oracle_runner = getattr(self, "oracle_runner", None) or OracleEvaluationRunner()
        return oracle_runner.evaluate_run(
            oracle_package,
            run,
            oracle_task=oracle_task,
            sealed_payload=sealed_payload,
        )

    def _oracle_claim_score(
        self,
        oracle_task: OracleTask,
        validator_results: Sequence[ValidatorResult],
        claim_results: Sequence[ClaimResult],
    ) -> float:
        oracle_package = getattr(self, "oracle_package", None)
        if oracle_package is None:
            return 0.0
        claim_ids = [str(claim_id) for claim_id in oracle_task.claim_ids]
        result_by_claim = {str(result.claim_id): result for result in claim_results}
        hard_claim_ids = set(getattr(oracle_package.scoring_projection, "hard_claim_ids", []) or []) & set(claim_ids)
        if any(result_by_claim.get(claim_id) is None or result_by_claim[claim_id].satisfied is not True for claim_id in hard_claim_ids):
            return 0.0

        claim_weights = dict(getattr(oracle_package.scoring_projection, "claim_weights", {}) or {})
        numerator = 0.0
        denominator = 0.0
        for claim_id in claim_ids:
            weight = max(0.0, float(claim_weights.get(claim_id, 1.0)))
            if weight <= 0.0:
                continue
            denominator += weight
            result = result_by_claim.get(claim_id)
            numerator += weight * (1.0 if result is not None and result.satisfied is True else 0.0)
        if denominator > 0.0:
            return numerator / denominator

        if not validator_results:
            return 0.0
        passed = sum(1 for result in validator_results if result.status == "pass")
        return passed / len(validator_results)

    def _score_oracle_results(
        self,
        runs: Sequence[RunResult],
        *,
        partition: str,
        tasks: Sequence[BenchmarkTask],
    ) -> list[RunResult]:
        oracle_map = self._oracle_task_by_runtime_task_id(partition, allow_train_fallback=True)
        if not oracle_map:
            return self._rescore_private_results(runs, tasks)
        rescored: list[RunResult] = []
        for run in runs:
            oracle_task = oracle_map.get(str(run.task_id))
            if oracle_task is None or run.hard_invalid:
                rescored.append(run)
                continue
            validator_results, claim_results = self._oracle_evidence_for_run(run, oracle_task=oracle_task)
            rescored.append(
                run.model_copy(
                    update={
                        "verifier_score": self._oracle_claim_score(oracle_task, validator_results, claim_results),
                    }
                )
            )
        return rescored

    def _rescore_private_results(self, runs: Sequence[RunResult], tasks: Sequence[BenchmarkTask]) -> list[RunResult]:
        return rescore_private_run_results(runs, tasks)

    def evaluate_runtime(
        self,
        runtime_dir: str | Path,
        partition: str = "train",
        seeds: Sequence[int] = (0, 1, 2),
        use_cache: bool = True,
        tasks_override: Sequence[Any] | None = None,
        *,
        use_reference_scales: bool = True,
        trace_context: OpenAITraceContext | None = None,
    ) -> SuiteEvaluation:
        runtime_profile = self._effective_runtime_profile(runtime_dir)
        runtime = self._load_runtime(runtime_dir, runtime_profile=runtime_profile)
        oracle_package = getattr(self, "oracle_package", None)
        oracle_hash = str(getattr(oracle_package, "package_hash", "") or "")
        tasks = self._resolve_evaluation_tasks(partition, tasks_override)
        task_key = tuple(
            stable_hash(sealed_benchmark_task_payload(task) if isinstance(task, BenchmarkTask) else (task).model_dump())
            for task in tasks
        )
        cache_key = (runtime.runtime_hash, partition, tuple(seeds), task_key, oracle_hash)
        if use_cache and cache_key in self.cache:
            return self.cache[cache_key]
        units = self._evaluation_units(tasks)
        run_results = []
        self.predictors.freeze()
        try:
            task_runs = [
                (task, int(seed))
                for seed in seeds
                for unit in units
                for task in unit
            ]
            batch_response = self.runtime_host.run_batch(
                runtime_dir,
                task_runs,
                provider=self.provider,
                runtime_profile=runtime_profile,
                budget_overrides=self.budget_overrides,
                trace_context=trace_context or self.trace_context,
            )
            self.last_provider_usage = dict(batch_response.provider_usage)
            run_results.extend(self._score_oracle_results(batch_response.run_results, partition=partition, tasks=tasks))
        finally:
            self.predictors.unfreeze()
        oracle_task_by_id = self._oracle_task_by_runtime_task_id(partition, allow_train_fallback=True)
        task_family_map = {task.task_id: task.family for task in tasks}
        task_metadata = {
            task.task_id: dict(
                oracle_task_by_id[task.task_id].benchmark_task.metadata
                if task.task_id in oracle_task_by_id
                else task.metadata
            )
            for task in tasks
        }
        evaluation_identity = self._evaluation_identity(runtime)
        evaluation = self._score_calculator(use_reference_scales=use_reference_scales).suite_score(
            runtime.runtime_hash,
            task_family_map,
            run_results,
            task_metadata=task_metadata,
            evaluation_identity=evaluation_identity,
        )
        if use_cache:
            self.cache[cache_key] = evaluation
        return evaluation

    def _apply_patch_uniquely(self, parent_dir: Path, candidate: MutationCandidate) -> Path:
        runtime = self._load_runtime(parent_dir)
        child_dir = ensure_directory(self.workspace / f"patched_{stable_hash(parent_dir, candidate.patch_text)[:10]}")
        if child_dir.exists():
            shutil.rmtree(child_dir)
        shutil.copytree(parent_dir, child_dir)
        allowed_files = {
            runtime.manifest.policy_modules[scope].split(":", 1)[0]
            for scope in candidate.touched_scope
            if scope in runtime.manifest.policy_modules
        }
        allowed_methods_by_file = self._allowed_methods_by_file(runtime, candidate.touched_scope)
        blocks = parse_patch(candidate.patch_text)
        touched_files: set[str] = set()
        for block in blocks:
            matches = []
            for rel_path in runtime.manifest.mutable_files:
                if allowed_files and rel_path not in allowed_files:
                    continue
                path = child_dir / rel_path
                source = path.read_text(encoding="utf-8")
                count = source.count(block.search)
                if count == 1:
                    matches.append(path)
                elif count > 1:
                    raise PatchApplyError(f"SEARCH block matched multiple locations in {rel_path}")
            if len(matches) != 1:
                raise PatchApplyError("SEARCH block must match exactly one mutable file")
            path = matches[0]
            rel_path = str(path.relative_to(child_dir)).replace("\\", "/")
            source = path.read_text(encoding="utf-8")
            self._ensure_patch_within_mutable_methods(source, block.search, allowed_methods_by_file.get(rel_path, set()))
            path.write_text(source.replace(block.search, block.replace, 1), encoding="utf-8")
            touched_files.add(rel_path)
        for rel_path in touched_files:
            allowed_methods = allowed_methods_by_file.get(rel_path, set())
            parent_source = (parent_dir / rel_path).read_text(encoding="utf-8")
            child_source = (child_dir / rel_path).read_text(encoding="utf-8")
            self._ensure_only_allowed_methods_changed(parent_source, child_source, allowed_methods)
        return child_dir

    def _patch_stats(self, patch_text: str) -> dict[str, int]:
        blocks = parse_patch(patch_text)
        changed_lines = 0
        for block in blocks:
            search_lines = [line for line in block.search.splitlines() if line.strip()]
            replace_lines = [line for line in block.replace.splitlines() if line.strip()]
            changed_lines += max(len(search_lines), len(replace_lines))
        return {"blocks": len(blocks), "changed_lines": changed_lines}

    def _method_ranges(self, source: str, method_names: Sequence[str]) -> list[tuple[int, int]]:
        tree = ast.parse(source)
        ranges: list[tuple[int, int]] = []
        allowed_names = set(method_names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in allowed_names:
                end_lineno = getattr(node, "end_lineno", node.lineno)
                ranges.append((node.lineno, end_lineno))
        return ranges

    def _ensure_patch_within_mutable_methods(self, source: str, search_text: str, allowed_methods: Sequence[str]) -> None:
        if not allowed_methods:
            raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")
        start = source.find(search_text)
        if start < 0:
            raise PatchApplyError("SEARCH block not found")
        start_line = source[:start].count("\n") + 1
        end_line = start_line + search_text.count("\n")
        for method_start, method_end in self._method_ranges(source, allowed_methods):
            if method_start <= start_line and end_line <= method_end:
                return
        raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")

    def _allowed_methods_by_file(self, runtime, touched_scope: Sequence[str]) -> dict[str, set[str]]:
        return {
            runtime.manifest.policy_modules[scope].split(":", 1)[0]: set(METHOD_CONTRACTS.get(scope, []))
            for scope in touched_scope
            if scope in runtime.manifest.policy_modules
        }

    def stage0_patch_integrity(self, parent_dir: Path, candidate: MutationCandidate) -> tuple[EvaluationStageResult, Path | None]:
        try:
            stats = self._patch_stats(candidate.patch_text)
            if stats["blocks"] > 4:
                raise PatchApplyError("patch exceeded max block count")
            if stats["changed_lines"] > 60:
                raise PatchApplyError("patch exceeded max changed lines")
            if any(len(block.search.splitlines()) > 8 for block in parse_patch(candidate.patch_text)):
                raise PatchApplyError("SEARCH block exceeded max 8 lines")
            child_dir = self._apply_patch_uniquely(parent_dir, candidate)
            runtime = self._load_runtime(child_dir)
            parent_runtime = self._load_runtime(parent_dir)
            allowed_methods_by_file = self._allowed_methods_by_file(parent_runtime, candidate.touched_scope)
            for rel_path in runtime.manifest.mutable_files:
                parent_source = (parent_dir / rel_path).read_text(encoding="utf-8")
                child_source = (child_dir / rel_path).read_text(encoding="utf-8")
                ast.parse(child_source)
                allowed_methods = allowed_methods_by_file.get(rel_path, set())
                if allowed_methods:
                    self._ensure_only_allowed_methods_changed(parent_source, child_source, allowed_methods)
                elif parent_source != child_source:
                    raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")
            return EvaluationStageResult(stage=0, passed=True, reason="patch applied and parsed", metrics=stats), child_dir
        except Exception as exc:
            return EvaluationStageResult(stage=0, passed=False, reason=str(exc)), None

    def stage1_smoke(self, child_dir: Path) -> EvaluationStageResult:
        smoke_task = self._partition_tasks("proxy", allow_train_fallback=True)[0]
        runs: list[tuple[SuiteEvaluation, Any, list[dict[str, Any]]]] = []
        for _ in range(max(2, self.stage1_replays)):
            evaluation = self.evaluate_runtime(child_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[smoke_task])
            run = evaluation.run_results[0]
            trace = self._normalize_trace(self._trace_rows(run))
            runs.append((evaluation, run, trace))
        baseline_eval, baseline_run, baseline_trace = runs[0]
        passed = True
        for evaluation, run, trace in runs[1:]:
            passed = passed and (
                not baseline_eval.invalid
                and not evaluation.invalid
                and baseline_run.artifact == run.artifact
                and baseline_run.verifier_score == run.verifier_score
                and baseline_run.mode == run.mode
                and baseline_trace == trace
            )
        reason = "deterministic smoke passed" if passed else "smoke task nondeterministic or invalid"
        return EvaluationStageResult(
            stage=1,
            passed=passed,
            reason=reason,
            metrics={"artifact": baseline_run.artifact, "mode": baseline_run.mode, "trace_events": len(baseline_trace)},
        )

    def stage2_proxy(self, parent_dir: Path, child_dir: Path, scope: Sequence[str], epsilon_proxy: float | None = None) -> EvaluationStageResult:
        epsilon_proxy = self.epsilon_proxy if epsilon_proxy is None else epsilon_proxy
        seeds = self.reference_profile.evaluation.proxy_seeds
        proxy_candidates = self._partition_tasks("proxy", allow_train_fallback=True)
        proxy_tasks = [task for task in proxy_candidates if set(task.proxy_scope_tags) & set(scope)]
        if not proxy_tasks:
            proxy_tasks = proxy_candidates[:1]
        parent_eval = self.evaluate_runtime(parent_dir, partition="proxy", seeds=seeds, tasks_override=proxy_tasks)
        child_eval = self.evaluate_runtime(child_dir, partition="proxy", seeds=seeds, tasks_override=proxy_tasks)
        parent_scores = [parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in proxy_tasks]
        child_scores = [child_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in proxy_tasks]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = lcb > -epsilon_proxy and not child_eval.invalid
        return EvaluationStageResult(stage=2, passed=passed, reason="proxy LCB gate", metrics={"delta": avg, "se": se, "lcb": lcb}, suite_evaluation=child_eval)

    def _objective_subset(self, objective: ObjectiveSpec) -> list[Any]:
        train_tasks = self._partition_tasks("train", allow_train_fallback=True)
        if objective.kind == ObjectiveKind.SINGLE_TASK and objective.task_id:
            target = next((task for task in train_tasks if task.task_id == objective.task_id), self.suite.by_id(objective.task_id))
            same_family = [task for task in train_tasks if task.family == target.family and task.task_id != target.task_id][:2]
            return [target] + same_family
        if objective.kind in {ObjectiveKind.FAMILY, ObjectiveKind.FAMILY_ROBUST} and objective.family:
            selected = [task for task in train_tasks if task.family == objective.family][:4]
            return selected or self.suite.representative_family_tasks(objective.family, partition="train", limit=4)
        return [next(task for task in train_tasks if task.family == family) for family in ["top", "mem", "tool", "e2e"]]

    def stage3_local_subset(self, parent_dir: Path, child_dir: Path, objective: ObjectiveSpec, epsilon_part: float | None = None) -> EvaluationStageResult:
        epsilon_part = self.epsilon_part if epsilon_part is None else epsilon_part
        seeds = self.reference_profile.evaluation.subset_seeds
        subset = self._objective_subset(objective)
        parent_eval = self.evaluate_runtime(parent_dir, partition="train", seeds=seeds, tasks_override=subset)
        child_eval = self.evaluate_runtime(child_dir, partition="train", seeds=seeds, tasks_override=subset)
        parent_scores = [parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in subset]
        child_scores = [child_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in subset]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = lcb > -epsilon_part and not child_eval.invalid
        return EvaluationStageResult(stage=3, passed=passed, reason="local subset LCB gate", metrics={"delta": avg, "se": se, "lcb": lcb}, suite_evaluation=child_eval)

    def stage4_full(
        self,
        parent_dir: Path,
        child_dir: Path,
        epsilon_full: float | None = None,
        objective: ObjectiveSpec | None = None,
    ) -> EvaluationStageResult:
        epsilon_full = self.epsilon_full if epsilon_full is None else epsilon_full
        seeds = self.reference_profile.evaluation.full_train_seeds
        stage4_tasks = self._partition_tasks("train", allow_train_fallback=True)
        parent_eval = self.evaluate_runtime(parent_dir, partition="train", seeds=seeds, tasks_override=stage4_tasks)
        oracle_task_by_id = self._oracle_task_by_runtime_task_id("train", allow_train_fallback=True)
        task_family_map = {task.task_id: task.family for task in stage4_tasks}
        task_metadata = {
            task.task_id: dict(
                oracle_task_by_id[task.task_id].benchmark_task.metadata
                if task.task_id in oracle_task_by_id
                else task.metadata
            )
            for task in stage4_tasks
        }
        child_evaluation_identity: dict[str, Any] = {}
        aggregated_runs = []
        parent_scores_accum: list[float] = []
        child_scores_accum: list[float] = []
        for batch in self._train_batches():
            child_batch = self.evaluate_runtime(child_dir, partition="train", seeds=seeds, use_cache=False, tasks_override=batch)
            if not child_evaluation_identity:
                child_evaluation_identity = dict(child_batch.evaluation_identity)
            if child_batch.invalid:
                decision = self._stage4_decision(parent_eval, child_batch)
                decision = self._write_stage4_ledgers(parent_eval, child_batch, decision)
                return self._stage4_result_from_decision(
                    decision,
                    epsilon_full=epsilon_full,
                    child_eval=child_batch,
                    reason_prefix="full train evaluation invalid",
                )
            aggregated_runs.extend(child_batch.run_results)
            parent_scores_accum.extend(parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in batch)
            child_scores_accum.extend(child_batch.objective_scores.get(f"s:{task.task_id}", 0.0) for task in batch)
            avg_batch, se_batch, _ = mean_improvement(child_scores_accum, parent_scores_accum)
            if avg_batch + 1.96 * se_batch < -self.delta_rej:
                try:
                    runtime_hash = self._load_runtime(child_dir).runtime_hash
                except Exception:
                    runtime_hash = str(child_dir)
                partial_eval = self._score_calculator().suite_score(
                    runtime_hash,
                    task_family_map,
                    aggregated_runs,
                    task_metadata=task_metadata,
                    evaluation_identity=child_evaluation_identity,
                )
                health_floor_status, leakage_status = self._stage4_contract_attestation(partial_eval)
                decision = self.progress_oracle.reject_evaluations(
                    parent_eval,
                    partial_eval,
                    contract=self.evidence_contract,
                    health_floor_status=health_floor_status,
                    leakage_status=leakage_status,
                    reason_codes=["stage4_early_rejection"],
                )
                decision = self._write_stage4_ledgers(parent_eval, partial_eval, decision)
                result = self._stage4_result_from_decision(
                    decision,
                    epsilon_full=epsilon_full,
                    child_eval=partial_eval,
                    reason_prefix="stage4 early rejection",
                )
                result.metrics.update({"ucb": avg_batch + 1.96 * se_batch, "delta_rej": self.delta_rej})
                return EvaluationStageResult(
                    stage=4,
                    passed=False,
                    reason="stage4 early rejection",
                    metrics=result.metrics,
                    suite_evaluation=partial_eval,
                    progress_signal=result.progress_signal,
                    promotion_decision=result.promotion_decision,
                    promotion_type=result.promotion_type,
                    promotion_decision_ref=result.promotion_decision_ref,
                    progress_signal_ref=result.progress_signal_ref,
                    evidence_contract_id=result.evidence_contract_id,
                    oracle_package_hash=result.oracle_package_hash,
                    runtime_spec_digest=result.runtime_spec_digest,
                )
        try:
            runtime_hash = self._load_runtime(child_dir).runtime_hash
        except Exception:
            runtime_hash = str(child_dir)
        child_eval = self._score_calculator().suite_score(
            runtime_hash,
            task_family_map,
            aggregated_runs,
            task_metadata=task_metadata,
            evaluation_identity=child_evaluation_identity,
        )
        decision = self._stage4_decision(parent_eval, child_eval)
        decision = self._write_stage4_ledgers(parent_eval, child_eval, decision)
        return self._stage4_result_from_decision(decision, epsilon_full=epsilon_full, child_eval=child_eval)

    def evaluate_validation(self, runtime_dir: Path) -> SuiteEvaluation:
        return self.evaluate_runtime(runtime_dir, partition="val", seeds=self.reference_profile.evaluation.validation_seeds)

    def staged_evaluate(self, parent_dir: Path, candidate: MutationCandidate, objective: ObjectiveSpec) -> tuple[list[EvaluationStageResult], Path | None]:
        results: list[EvaluationStageResult] = []
        stage0, child_dir = self.stage0_patch_integrity(parent_dir, candidate)
        results.append(stage0)
        if not stage0.passed or child_dir is None:
            return results, None
        stage1 = self.stage1_smoke(child_dir)
        results.append(stage1)
        if not stage1.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage2 = self.stage2_proxy(parent_dir, child_dir, candidate.touched_scope, epsilon_proxy=self.epsilon_proxy)
        results.append(stage2)
        if not stage2.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage3 = self.stage3_local_subset(parent_dir, child_dir, objective, epsilon_part=self.epsilon_part)
        results.append(stage3)
        if not stage3.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage4 = self.stage4_full(parent_dir, child_dir, objective=objective)
        results.append(stage4)
        if not stage4.passed:
            self._cleanup_path(child_dir, failed=True)
        return results, child_dir

    def staged_evaluate_runtime_pair(
        self,
        parent_dir: Path,
        child_dir: Path,
        objective: ObjectiveSpec,
        *,
        scope: Sequence[str] = ("top", "mem", "tool", "ctl"),
        mutation_action_ids: Sequence[str] = (),
    ) -> tuple[list[EvaluationStageResult], Path | None]:
        action_ids = [str(action_id) for action_id in mutation_action_ids]
        results: list[EvaluationStageResult] = []
        stage1 = self.stage1_smoke(child_dir).model_copy(update={"mutation_action_ids": action_ids})
        results.append(stage1)
        if not stage1.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage2 = self.stage2_proxy(parent_dir, child_dir, scope, epsilon_proxy=self.epsilon_proxy).model_copy(update={"mutation_action_ids": action_ids})
        results.append(stage2)
        if not stage2.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage3 = self.stage3_local_subset(parent_dir, child_dir, objective, epsilon_part=self.epsilon_part).model_copy(update={"mutation_action_ids": action_ids})
        results.append(stage3)
        if not stage3.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage4 = self.stage4_full(parent_dir, child_dir, objective=objective).model_copy(update={"mutation_action_ids": action_ids})
        results.append(stage4)
        if not stage4.passed:
            self._cleanup_path(child_dir, failed=True)
        return results, child_dir
