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
    freeze_oracle_package,
    validate_runtime_spec_payload,
)
from ..contracts.execution import OperationSpec
from ..utils import stable_hash
from .qa import OracleQARunner
from .validator_registry import ValidatorRegistry, default_validator_registry


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
        return {
            "goal_id": goal.goal_id,
            "goal_text": self._goal_text(goal),
            "runtime_spec_digest": getattr(runtime_spec, "spec_digest", "") or "",
            "private_expected_available": True,
            "trace_available": True,
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
            if family.score_applicability(context) > 0.0:
                proposed.append(family)
        if proposed:
            return proposed[:8]
        return self.registry.select(context, minimum_score=0.1, limit=8)

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
        claims = [
            ClaimSpec(
                claim_id="claim.goal_outcome",
                text="The runtime solves the user goal on representative public tasks and sealed variants.",
                claim_type="outcome",
                criticality="hard",
                weight=1.0,
                minimum_authority="A4",
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
                criticality="hard",
                weight=1.0,
                minimum_authority="A4",
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
        for family in families:
            if family.family_id == "pairwise_preference" and not context.get("requires_human_audit"):
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
            if not claim_ids:
                continue
            validator_id = self.registry.make_validator_id(family.family_id, claim_ids, {"goal": context["goal_id"]})
            validators.append(family.make_spec(validator_id=validator_id, claim_ids=claim_ids, inputs={"goal_id": context["goal_id"]}))
        if not any("claim.no_leakage" in validator.claim_ids for validator in validators):
            family = self.registry.get("trace_state")
            validators.append(family.make_spec(validator_id=self.registry.make_validator_id("trace_state", ["claim.no_leakage"], {}), claim_ids=["claim.no_leakage"], inputs={"required_events": []}))
        return validators

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
                minimum_authority=claim.minimum_authority,
                failure_action="reject" if claim.criticality == "hard" else "abstain",
            )
            for claim in claims
        ]


def compile_oracle_package(goal: GoalSpec | dict[str, Any], runtime_spec: RuntimeSpec | dict[str, Any] | None = None) -> OraclePackage:
    return OracleCompiler().compile(goal, runtime_spec)


__all__ = ["OracleCompiler", "OracleCompilerConfig", "compile_oracle_package"]
