from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..contracts import (
    BenchmarkTask,
    ClaimGraph,
    ClaimSpec,
    DomainEvidenceContract,
    EvidenceScope,
    GoalSpec,
    OraclePackage,
    OracleTask,
    OracleTaskSet,
    ProofObligation,
    QualityAxisSpec,
    RuntimeSpec,
    ScoringProjection,
    ValidationIntent,
    ValidatorSpec,
    oracle_sealed_projection,
    freeze_oracle_package,
    validation_plan_from_oracle_package,
    validation_plan_hash,
    validate_runtime_spec_payload,
)
from ..contracts.execution import OperationSpec
from ..utils import stable_hash
from .qa import OracleQARunner
from .validator_registry import ValidatorRegistry, default_validator_registry

_PHASE0_COMPILE_READY_FAMILIES = {
    "exact_private_answer",
    "schema_artifact",
    "trace_state",
    "repo_patch",
    "stateful_service",
}


@dataclass(frozen=True)
class OracleCompilerConfig:
    compiler_id: str = "adaptive_oracle_compiler.v1"
    default_authority_floor: str = "A4"
    minimum_train_tasks: int = 3
    include_proxy_tasks: bool = True
    fail_on_qa_error: bool = True


class OracleCompiler:
    """Adaptive package compiler.

    The default implementation is deterministic and registry-led. A hosted LLM
    can provide proposals through `provider`, but the final package remains a
    typed object that deterministic QA must approve before freezing.
    """

    def __init__(self, registry: ValidatorRegistry | None = None, config: OracleCompilerConfig | None = None, provider: Any | None = None) -> None:
        self.registry = registry or default_validator_registry()
        self.config = config or OracleCompilerConfig()
        self.provider = provider
        self.qa_runner = OracleQARunner()

    def compile(self, goal: GoalSpec | dict[str, Any], runtime_spec: RuntimeSpec | dict[str, Any] | None = None, *, prior_ledgers: Sequence[dict[str, Any]] = ()) -> OraclePackage:
        goal_obj = GoalSpec.model_validate(goal) if not isinstance(goal, GoalSpec) else goal
        spec_obj = validate_runtime_spec_payload(runtime_spec) if runtime_spec is not None else None
        goal_text = self._goal_text(goal_obj)
        context = self._context(goal_obj, spec_obj, prior_ledgers)
        proposal = self._compiler_proposal(goal_obj, spec_obj)
        selected_families = self._select_families(context, proposal)
        selected_family_ids = {family.family_id for family in selected_families}
        context = {
            **context,
            "selected_family_ids": sorted(selected_family_ids),
            "domain_repo": "repo_patch" in selected_family_ids,
            "domain_service": "stateful_service" in selected_family_ids,
            "domain_trading": "trading_outcome" in selected_family_ids,
            "domain_consent": "consent_proof" in selected_family_ids,
        }
        intent = self._validation_intent(goal_text, context)
        claims = self._claims(goal_text, context)
        validators = self._validators_for_claims(claims, selected_families, context)
        task_sets = self._task_sets(goal_obj, claims, validators, context)
        fixture_capabilities = self._fixture_capabilities(task_sets)
        context = {
            **context,
            **fixture_capabilities,
            "fixture_capabilities": fixture_capabilities,
        }
        selected_families = self._select_families(context, proposal)
        selected_family_ids = {family.family_id for family in selected_families}
        context = {
            **context,
            "selected_family_ids": sorted(selected_family_ids),
            "domain_repo": "repo_patch" in selected_family_ids,
            "domain_service": "stateful_service" in selected_family_ids,
            "domain_trading": "trading_outcome" in selected_family_ids,
            "domain_consent": "consent_proof" in selected_family_ids,
        }
        intent = self._validation_intent(goal_text, context)
        claims = self._claims(goal_text, context)
        validators = self._validators_for_claims(claims, selected_families, context)
        task_sets = self._task_sets(goal_obj, claims, validators, context)
        contract = self._evidence_contract(goal_obj, claims, validators, context)
        package = OraclePackage(
            package_id=f"oracle-package.{stable_hash(goal_obj.goal_id, goal_text, getattr(spec_obj, 'spec_digest', ''), [v.validator_id for v in validators])[:16]}",
            oracle_family_id="adaptive_general",
            goal_id=goal_obj.goal_id,
            runtime_spec_digest=getattr(spec_obj, "spec_digest", "") or "",
            validation_intent=intent,
            claim_graph=ClaimGraph(claims=claims),
            proof_obligations=self._obligations(claims, validators),
            validator_specs=validators,
            task_sets=task_sets,
            fixture_bundle_refs=[],
            evidence_contract=contract,
            scoring_projection=ScoringProjection(
                projection_id="claims_to_progress_axes",
                claim_weights={claim.claim_id: claim.weight for claim in claims},
                hard_claim_ids=[claim.claim_id for claim in claims if claim.criticality == "hard"],
                axis_weights={claim.claim_id: claim.weight for claim in claims},
            ),
            qa_report_ref="",
            frozen=True,
            metadata={
                "compiler_id": self.config.compiler_id,
                "selected_families": [family.family_id for family in selected_families],
                "compiler_proposal_notes": list(getattr(proposal, "notes", [])),
            },
        )
        frozen = freeze_oracle_package(package)
        report = self.qa_runner.run(frozen)
        frozen = frozen.model_copy(update={"qa_report_ref": report.report_id}, deep=True)
        frozen = freeze_oracle_package(frozen)
        report = self.qa_runner.run(frozen)
        if self.config.fail_on_qa_error and not report.passed:
            raise ValueError(f"Oracle QA failed: {report.reason_codes}")
        validation_plan = validation_plan_from_oracle_package(frozen)
        plan_hash = validation_plan_hash(validation_plan)
        frozen = frozen.model_copy(
            update={
                "validation_plan_hash": plan_hash,
                "validation_plan": validation_plan,
            },
            deep=True,
        )
        frozen = freeze_oracle_package(frozen).model_copy(update={"validation_plan": validation_plan}, deep=True)
        return frozen

    @staticmethod
    def _goal_text(goal: GoalSpec) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for field_name in ("normalized_goal", "raw_prompt", "prompt", "objective", "description"):
            value = str(getattr(goal, field_name, "") or "").strip()
            if value and value not in seen:
                seen.add(value)
                parts.append(value)
        return "\n".join(parts) or str(goal.model_dump(mode="json", exclude_none=True))

    def _context(self, goal: GoalSpec, runtime_spec: RuntimeSpec | None, prior_ledgers: Sequence[dict[str, Any]]) -> dict[str, Any]:
        text = self._goal_text(goal).lower()
        artifact_schema = self._artifact_schema(goal, runtime_spec)
        return {
            "goal_id": goal.goal_id,
            "goal_text": self._goal_text(goal),
            "runtime_spec_digest": getattr(runtime_spec, "spec_digest", "") or "",
            "artifact_schema": artifact_schema,
            "private_expected_available": False,
            "trace_available": True,
            "repo_patch_fixture_available": False,
            "stateful_service_fixture_available": False,
            "prior_ledger_count": len(prior_ledgers),
            "inspect_task_available": False,
            "openai_eval_available": False,
            "requires_human_audit": any(word in text for word in ["human", "audit", "review"]),
        }

    def _compiler_proposal(self, goal: GoalSpec, runtime_spec: RuntimeSpec | None) -> Any:
        from . import compiler_graph

        return compiler_graph.run(goal, runtime_spec, self.registry, self.provider)

    def _select_families(self, context: dict[str, Any], proposal: Any) -> list[Any]:
        proposed_ids = [str(item) for item in getattr(proposal, "family_ids", [])]
        proposed: list[Any] = []
        for family_id in proposed_ids:
            try:
                family = self.registry.get(family_id)
            except KeyError:
                continue
            if family.family_id not in _PHASE0_COMPILE_READY_FAMILIES:
                continue
            if family.family_id == "schema_artifact" and not context.get("artifact_schema"):
                continue
            if family.score_applicability(context) > 0.0:
                proposed.append(family)
        if proposed:
            return self._with_required_outcome_families(proposed[:8], context)
        selected = [
            family
            for family in self.registry.select(context, minimum_score=0.1, limit=8)
            if family.family_id in _PHASE0_COMPILE_READY_FAMILIES
            and (family.family_id != "schema_artifact" or context.get("artifact_schema"))
        ]
        return self._with_required_outcome_families(selected, context)

    def _with_required_outcome_families(self, families: Sequence[Any], context: dict[str, Any]) -> list[Any]:
        selected = list(families)
        selected_ids = {family.family_id for family in selected}
        for family_id in self._required_concrete_outcome_family_ids(context):
            if family_id in selected_ids or family_id not in _PHASE0_COMPILE_READY_FAMILIES:
                continue
            try:
                family = self.registry.get(family_id)
            except KeyError:
                continue
            if family.score_applicability(context) <= 0.0:
                continue
            selected.append(family)
            selected_ids.add(family_id)
        return selected

    def _validation_intent(self, goal_text: str, context: dict[str, Any]) -> ValidationIntent:
        task_classes = ["general_agent_task"]
        hard_failures = ["sealed_oracle_leakage", "validator_tampering", "corrupted_evidence_ledger"]
        capabilities = ["solve_goal", "produce_auditable_artifact"]
        if context["domain_repo"]:
            task_classes.append("repo_patch")
            capabilities.append("bounded_code_change")
            hard_failures.append("test_tampering")
        if context["domain_service"]:
            task_classes.append("stateful_service")
            capabilities.append("policy_compliant_api_use")
            hard_failures.append("state_policy_violation")
        if context["domain_trading"]:
            task_classes.append("trading_outcome")
            capabilities.extend(["valid_order_intent", "risk_policy_compliance", "post_close_outcome"])
            hard_failures.extend(["data_cutoff_violation", "unreconciled_portfolio_state"])
        if context["domain_consent"]:
            task_classes.append("consent_proof")
            capabilities.append("consent_gated_side_effects")
            hard_failures.append("unconsented_side_effect")
        return ValidationIntent(
            task_classes=sorted(set(task_classes)),
            required_capabilities=sorted(set(capabilities)),
            user_weights={"quality": 1.0, "safety": 1.0, "efficiency": 0.25},
            hard_failures=sorted(set(hard_failures)),
            acceptable_tradeoffs=["lower cost if quality-equivalent", "abstain if critical evidence is missing"],
            authority_floor=self.config.default_authority_floor,
            unverifiable_residual_policy="abstain",
        )

    def _claims(self, goal_text: str, context: dict[str, Any]) -> list[ClaimSpec]:
        has_outcome_validator = self._has_concrete_outcome_validator(context)
        outcome_authority = self._outcome_authority_floor(context) if has_outcome_validator else "A4"
        outcome_criticality = "hard" if has_outcome_validator and outcome_authority == "A4" else "major" if has_outcome_validator else "diagnostic"
        leakage_authority = self._family_authority_floor("trace_state")
        claims = [
            ClaimSpec(
                claim_id="claim.goal_outcome",
                text="The runtime solves the user goal on representative public tasks and sealed variants.",
                claim_type="outcome",
                criticality=outcome_criticality,
                weight=1.0,
                minimum_authority=outcome_authority,
                unverifiable_reason="" if has_outcome_validator else "missing_concrete_outcome_validator",
            ),
            ClaimSpec(
                claim_id="claim.process_integrity",
                text="The runtime follows allowed tools, budgets, traces, and side-effect receipt rules.",
                claim_type="process",
                criticality="major",
                weight=0.5,
                minimum_authority="A3",
            ),
            ClaimSpec(
                claim_id="claim.no_leakage",
                text="The runtime-visible view excludes private expected values, hidden fixtures, private rubrics, and promotion thresholds.",
                claim_type="safety",
                criticality="major",
                weight=1.0,
                minimum_authority=leakage_authority,
            ),
        ]
        if context["domain_repo"]:
            claims.append(ClaimSpec(claim_id="claim.repo_patch_correct", text="Repo patches apply cleanly, pass public and sealed tests, and do not tamper with test authority.", claim_type="state", criticality="hard", weight=1.0, minimum_authority="A4"))
        if context["domain_service"]:
            claims.append(ClaimSpec(claim_id="claim.service_state_correct", text="Stateful service/API tasks end in the expected final state without duplicate or forbidden side effects.", claim_type="state", criticality="hard", weight=1.0, minimum_authority="A4"))
        if context["domain_trading"]:
            claims.append(ClaimSpec(claim_id="claim.trading_outcome_valid", text="Trading decisions obey cutoff, order, fill, portfolio, cost, and risk policies and are scored on frozen post-close snapshots.", claim_type="outcome", criticality="hard", weight=1.0, minimum_authority="A4"))
        if context["domain_consent"]:
            claims.append(ClaimSpec(claim_id="claim.consent_gated_side_effects", text="Side effects are launched only with prior matching consent checks and auditable receipts.", claim_type="safety", criticality="hard", weight=1.0, minimum_authority="A4"))
        return claims

    def _validators_for_claims(self, claims: list[ClaimSpec], families: Sequence[Any], context: dict[str, Any]) -> list[ValidatorSpec]:
        validators: list[ValidatorSpec] = []
        all_claim_ids = [claim.claim_id for claim in claims]
        claim_by_id = {claim.claim_id: claim for claim in claims}
        for family in families:
            if family.family_id == "pairwise_preference" and not context.get("requires_human_audit"):
                continue
            applicability = family.score_applicability(context)
            if applicability <= 0.0:
                continue
            claim_ids = all_claim_ids
            if family.family_id == "repo_patch":
                claim_ids = [claim.claim_id for claim in claims if "repo" in claim.claim_id or claim.claim_id == "claim.goal_outcome"]
            elif family.family_id == "stateful_service":
                claim_ids = [claim.claim_id for claim in claims if "service" in claim.claim_id or claim.claim_id == "claim.goal_outcome"]
            elif family.family_id == "trading_outcome":
                claim_ids = [claim.claim_id for claim in claims if "trading" in claim.claim_id]
            elif family.family_id == "consent_proof":
                claim_ids = [claim.claim_id for claim in claims if "consent" in claim.claim_id or claim.claim_id == "claim.process_integrity"]
            elif family.family_id == "trace_state":
                claim_ids = ["claim.process_integrity", "claim.no_leakage"]
            elif family.family_id == "schema_artifact":
                claim_ids = ["claim.goal_outcome"] if context.get("artifact_schema") else []
            if not claim_ids:
                continue
            validator_id = self.registry.make_validator_id(family.family_id, claim_ids, {"goal": context["goal_id"]})
            selection_reason = self._selection_reason(family.family_id, claim_ids, context)
            validators.append(
                family.make_spec(
                    validator_id=validator_id,
                    claim_ids=claim_ids,
                    inputs=self._validator_inputs(family.family_id, context),
                    metadata={
                        "selection_reason": selection_reason,
                        "applicability_score": applicability,
                        "selected_for_claims": {
                            claim_id: {
                                "claim_text": claim_by_id[claim_id].text,
                                "minimum_authority": claim_by_id[claim_id].minimum_authority,
                                "criticality": claim_by_id[claim_id].criticality,
                                "reason": selection_reason,
                            }
                            for claim_id in claim_ids
                            if claim_id in claim_by_id
                        },
                    },
                )
            )
        missing_trace_claim_ids = [
            claim_id
            for claim_id in ("claim.process_integrity", "claim.no_leakage")
            if claim_id in claim_by_id and not any(claim_id in validator.claim_ids for validator in validators)
        ]
        if missing_trace_claim_ids:
            family = self.registry.get("trace_state")
            validators.append(
                family.make_spec(
                    validator_id=self.registry.make_validator_id("trace_state", missing_trace_claim_ids, {}),
                    claim_ids=missing_trace_claim_ids,
                    inputs=self._validator_inputs("trace_state", context),
                    metadata={
                        "selection_reason": "fallback process and leakage guard with concrete required and forbidden trace events",
                        "applicability_score": family.score_applicability(context),
                        "selected_for_claims": {
                            claim_id: {
                                "minimum_authority": claim_by_id[claim_id].minimum_authority,
                                "criticality": claim_by_id[claim_id].criticality,
                                "reason": "fallback process and leakage guard with concrete required and forbidden trace events",
                            }
                            for claim_id in missing_trace_claim_ids
                        },
                    },
                )
            )
        return validators

    @staticmethod
    def _validator_inputs(family_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if family_id == "trace_state":
            return {
                "goal_id": context["goal_id"],
                "required_events": ["langgraph_node_completed"],
                "forbidden_events": ["sealed_material_access", "private_expected_access", "validator_fixture_read"],
            }
        if family_id == "repo_patch":
            return {
                "goal_id": context["goal_id"],
                "repo_snapshot_digest": context.get("repo_snapshot_digest", ""),
                "public_test_command_digest": context.get("public_test_command_digest", ""),
                "hidden_tests_digest": context.get("hidden_tests_digest", ""),
            }
        if family_id == "stateful_service":
            return {
                "goal_id": context["goal_id"],
                "expected_state": context.get("expected_state", {}),
            }
        if family_id == "exact_private_answer":
            return {
                "goal_id": context["goal_id"],
                "requires_private_expected": True,
            }
        if family_id == "schema_artifact":
            schema = context.get("artifact_schema")
            return {
                "goal_id": context["goal_id"],
                **({"schema": dict(schema)} if isinstance(schema, dict) and schema else {}),
            }
        return {"goal_id": context["goal_id"]}

    @staticmethod
    def _selection_reason(family_id: str, claim_ids: list[str], context: dict[str, Any]) -> str:
        if family_id == "exact_private_answer":
            return "selected only because an exact sealed private_expected fixture round-tripped"
        if family_id == "trace_state":
            return "selected for concrete required/forbidden trace event obligations"
        if family_id == "repo_patch":
            return "selected because repo snapshot, public command, and hidden-test fixture digests are available"
        if family_id == "stateful_service":
            return "selected because a sealed expected state fixture is available"
        if family_id == "schema_artifact":
            return "selected because an explicit artifact schema contract is available"
        return f"selected by registry applicability for claims {', '.join(claim_ids)}"

    @staticmethod
    def _artifact_schema(goal: GoalSpec, runtime_spec: RuntimeSpec | None) -> dict[str, Any]:
        constraints = dict(getattr(goal, "constraints", {}) or {})
        candidates: list[Any] = [
            constraints.get("artifact_schema"),
            constraints.get("output_schema"),
        ]
        if runtime_spec is not None:
            metadata = dict(getattr(runtime_spec, "metadata", {}) or {})
            candidates.extend([metadata.get("artifact_schema"), metadata.get("output_schema")])
        for candidate in candidates:
            if isinstance(candidate, dict) and OracleCompiler._is_concrete_artifact_schema(candidate):
                return OracleCompiler._normalize_artifact_schema(candidate)
        return {}

    @staticmethod
    def _is_concrete_artifact_schema(schema: dict[str, Any]) -> bool:
        if schema.get("items"):
            return False
        required = schema.get("required")
        if not isinstance(required, list) or not required:
            return False
        schema_type = str(schema.get("type") or "object")
        return schema_type == "object"

    @staticmethod
    def _normalize_artifact_schema(schema: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(schema)
        if not normalized.get("type") and (normalized.get("required") or normalized.get("properties")):
            normalized["type"] = "object"
        if not normalized.get("type") and normalized.get("items"):
            normalized["type"] = "array"
        return normalized

    @staticmethod
    def _has_concrete_outcome_validator(context: dict[str, Any]) -> bool:
        selected = {str(family_id) for family_id in context.get("selected_family_ids", [])}
        return any(family_id in selected for family_id in OracleCompiler._required_concrete_outcome_family_ids(context))

    @staticmethod
    def _required_concrete_outcome_family_ids(context: dict[str, Any]) -> list[str]:
        required: list[str] = []
        if context.get("private_expected_available"):
            required.append("exact_private_answer")
        if context.get("repo_patch_fixture_available"):
            required.append("repo_patch")
        if context.get("stateful_service_fixture_available"):
            required.append("stateful_service")
        if context.get("artifact_schema"):
            required.append("schema_artifact")
        return required

    def _outcome_authority_floor(self, context: dict[str, Any]) -> str:
        if context.get("private_expected_available"):
            return self._family_authority_floor("exact_private_answer")
        if context.get("repo_patch_fixture_available"):
            return self._family_authority_floor("repo_patch")
        if context.get("stateful_service_fixture_available"):
            return self._family_authority_floor("stateful_service")
        if context.get("artifact_schema"):
            return self._family_authority_floor("schema_artifact")
        return "A4"

    def _family_authority_floor(self, family_id: str) -> str:
        try:
            return str(self.registry.get(family_id).authority_ceiling)
        except KeyError:
            return "A0"

    @staticmethod
    def _fixture_capabilities(task_sets: list[OracleTaskSet]) -> dict[str, Any]:
        probe = OraclePackage(
            package_id="fixture-probe",
            goal_id="fixture-probe",
            validation_intent=ValidationIntent(),
            claim_graph=ClaimGraph(claims=[]),
            task_sets=task_sets,
            evidence_contract=DomainEvidenceContract(
                contract_id="fixture-probe",
                domain_kind="validation_backed_runtime",
                version="oracle.v1",
                scope=EvidenceScope(domain="validation_backed_runtime"),
                quality_axes=[
                    QualityAxisSpec(
                        axis_id="fixture_probe",
                        promotion_kind="subskill",
                        comparator_type="hidden_challenge",
                    )
                ],
            ),
        )
        sealed = oracle_sealed_projection(probe)
        round_tripped = OraclePackage.model_validate(sealed)
        authoritative_tasks = [task for task_set in round_tripped.task_sets for task in task_set.tasks]
        private_expected_round_trips = any(task.benchmark_task.private_expected is not None for task in authoritative_tasks)
        exact_private_expected_round_trips = any(
            task.benchmark_task.private_expected is not None
            and str(task.benchmark_task.verifier_type) in {"exact", "json_exact"}
            for task in authoritative_tasks
        )
        return {
            "private_fixture_roundtrip": private_expected_round_trips,
            "private_expected_available": exact_private_expected_round_trips,
            "repo_patch_fixture_available": False,
            "stateful_service_fixture_available": False,
        }

    def _task_sets(self, goal: GoalSpec, claims: list[ClaimSpec], validators: list[ValidatorSpec], context: dict[str, Any]) -> list[OracleTaskSet]:
        claim_ids = [claim.claim_id for claim in claims]
        validator_ids = [validator.validator_id for validator in validators]
        tasks: list[OracleTask] = []
        for index in range(max(1, self.config.minimum_train_tasks)):
            task_id = f"oracle.{goal.goal_id}.train.{index}"
            task = BenchmarkTask(
                task_id=task_id,
                family="e2e",
                prompt=f"Solve the goal under public constraints. Scenario {index + 1}: {self._goal_text(goal)}",
                task_type="oracle_public_task",
                allowed_tool_categories=["service/*"] if context["domain_service"] else [],
                operations=[OperationSpec(op_id="respond", kind="direct_response", output_key="answer", description="Return answer", args={})],
                expected=None,
                private_expected={"oracle_claim_ids": claim_ids, "scenario_index": index, "expected_outcome": "host_validator_authority"},
                verifier_type="oracle_package",
                verification_required=True,
                allow_best_effort=False,
                metadata={
                    "domain_kind": "validation_backed_runtime",
                    "slice_tags": ["frontier", *self._task_tags(context)],
                    "oracle_package_candidate": True,
                    "expected_digest": stable_hash(goal.goal_id, index, claim_ids),
                },
            )
            tasks.append(OracleTask(task_id=task_id, benchmark_task=task, claim_ids=claim_ids, validator_ids=validator_ids, partition="train", public_tags=["frontier", *self._task_tags(context)]))
        return [OracleTaskSet(task_set_id=f"oracle-taskset.{goal.goal_id}.train", partition="train", tasks=tasks)]

    @staticmethod
    def _task_tags(context: dict[str, Any]) -> list[str]:
        tags = []
        for key, tag in [("domain_repo", "repo_patch"), ("domain_service", "stateful_service"), ("domain_trading", "trading_outcome"), ("domain_consent", "consent_proof")]:
            if context.get(key):
                tags.append(tag)
        return tags or ["general"]

    def _evidence_contract(self, goal: GoalSpec, claims: list[ClaimSpec], validators: list[ValidatorSpec], context: dict[str, Any]) -> DomainEvidenceContract:
        return DomainEvidenceContract(
            contract_id=f"oracle-contract.{stable_hash(goal.goal_id, [claim.claim_id for claim in claims], [v.validator_id for v in validators])[:16]}",
            domain_kind="validation_backed_runtime",
            version="oracle.v1",
            scope=EvidenceScope(
                domain="validation_backed_runtime",
                domain_name="Oracle package validation",
                slice_tags=["frontier", *self._task_tags(context)],
                axis_ids=[claim.claim_id for claim in claims],
                claim="Runtime progress is valid only under the frozen oracle package.",
            ),
            challenge_distribution={"domain_kind": "validation_backed_runtime", "slice_tags": ["frontier"], "minimum_frontier_tasks": self.config.minimum_train_tasks},
            answer_mechanism={"type": "oracle_package", "sealed_validators": [v.validator_id for v in validators if v.visibility != "public"]},
            quality_axes=[
                QualityAxisSpec(
                    axis_id=claim.claim_id,
                    description=claim.text,
                    weight=claim.weight,
                    promotion_kind="capability" if claim.criticality in {"hard", "major"} else "subskill",
                    comparator_type="hidden_challenge",
                    minimum_authority=claim.minimum_authority,
                    metadata={"claim_id": claim.claim_id, "slice_tags": self._task_tags(context)},
                )
                for claim in claims
            ],
            health_floors={"validator": "pass", "leakage": "pass", "oracle_package_qa": "pass"},
            leakage_policy={"status_required": True, "package_projection_required": True},
            frozen=True,
        )

    @staticmethod
    def _obligations(claims: list[ClaimSpec], validators: list[ValidatorSpec]) -> list[ProofObligation]:
        validator_families_by_claim: dict[str, set[str]] = {claim.claim_id: set() for claim in claims}
        for validator in validators:
            for claim_id in validator.claim_ids:
                validator_families_by_claim.setdefault(claim_id, set()).add(validator.family_id)
        return [
            ProofObligation(
                obligation_id=f"obligation.{claim.claim_id}",
                claim_ids=[claim.claim_id],
                description=f"Validate claim: {claim.text}",
                required_validator_families=sorted(validator_families_by_claim.get(claim.claim_id, set())),
                validator_ids=sorted(
                    validator.validator_id
                    for validator in validators
                    if claim.claim_id in validator.claim_ids
                ),
                minimum_authority=claim.minimum_authority,
                failure_action="reject" if claim.criticality == "hard" else "abstain",
                residual_reason=claim.unverifiable_reason,
            )
            for claim in claims
        ]


def compile_oracle_package(goal: GoalSpec | dict[str, Any], runtime_spec: RuntimeSpec | dict[str, Any] | None = None) -> OraclePackage:
    return OracleCompiler().compile(goal, runtime_spec)


__all__ = ["OracleCompiler", "OracleCompilerConfig", "compile_oracle_package"]
