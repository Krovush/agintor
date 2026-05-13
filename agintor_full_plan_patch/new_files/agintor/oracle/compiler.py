from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..contracts import (
    AuthorityPolicy,
    ClaimGraph,
    ClaimSpec,
    DomainEvidenceContract,
    EvidenceScope,
    GoalSpec,
    OraclePackage,
    OracleTaskSet,
    ProofObligation,
    QualityAxisSpec,
    RuntimeSpec,
    ScoringProjection,
    ValidationIntent,
)
from ..utils import stable_hash
from .package_io import finalize_oracle_package
from .qa import assert_oracle_qa_passes
from .validator_registry import ValidatorRegistry, default_validator_registry


@dataclass(frozen=True)
class OracleCompilerConfig:
    oracle_family_id: str = "adaptive-general-v1"
    authority_floor: str = "A4"
    include_diagnostic_trace_validator: bool = True
    qa_required: bool = True


class OracleCompiler:
    """LLM-led compiler shell with deterministic fallback semantics.

    The deterministic implementation is intentionally conservative: it creates a
    frozen package from the GoalSpec, picks applicable registry families, and
    refuses high-authority claims that cannot be validated. An LLM graph can be
    injected later through compiler_graph without changing the package format.
    """

    def __init__(self, registry: ValidatorRegistry | None = None, config: OracleCompilerConfig | None = None) -> None:
        self.registry = registry or default_validator_registry()
        self.config = config or OracleCompilerConfig()

    def compile(
        self,
        goal_spec: GoalSpec,
        runtime_spec: RuntimeSpec | None = None,
        *,
        prior_ledgers: Sequence[dict[str, Any]] = (),
        task_sets: Sequence[OracleTaskSet] = (),
    ) -> OraclePackage:
        intent = self._validation_intent(goal_spec)
        claims = self._claim_specs(goal_spec, intent)
        validators = self.registry.build_validator_specs(intent, claims)
        if self.config.include_diagnostic_trace_validator and not any(v.family_id == "trace_state" for v in validators):
            validators.extend(self.registry.build_validator_specs(intent, claims, family_ids=["trace_state"]))
        claim_graph = ClaimGraph(
            graph_id=f"claims.{stable_hash(goal_spec.goal_id, intent.task_classes)[:12]}",
            claims=claims,
        )
        obligations = [
            ProofObligation(
                obligation_id=f"obl.{claim.claim_id}",
                claim_ids=[claim.claim_id],
                description=f"Validate claim: {claim.text}",
                required_authority=claim.minimum_authority,
                validator_family_hints=sorted({validator.family_id for validator in validators if claim.claim_id in validator.claim_ids}),
                failure_action="reject" if claim.criticality == "hard" else "abstain",
            )
            for claim in claims
        ]
        evidence_contract = self._evidence_contract(goal_spec, intent, claims)
        scoring_projection = ScoringProjection(
            projection_id=f"scoring.{stable_hash(goal_spec.goal_id, [claim.claim_id for claim in claims])[:12]}",
            axis_map={claim.claim_id: [claim.claim_id] for claim in claims},
            weights={claim.claim_id: float(claim.weight) for claim in claims},
            promotion_axes=[claim.claim_id for claim in claims if claim.criticality in {"hard", "major"}],
        )
        runtime_digest = runtime_spec.spec_digest if runtime_spec is not None else ""
        package = OraclePackage(
            package_id=f"oracle.{stable_hash(goal_spec.goal_id, runtime_digest, intent.model_dump(mode='json'))[:16]}",
            oracle_family_id=self.config.oracle_family_id,
            goal_id=goal_spec.goal_id,
            runtime_spec_digest=runtime_digest,
            validation_intent=intent,
            claim_graph=claim_graph,
            proof_obligations=obligations,
            validator_specs=validators,
            task_sets=list(task_sets),
            evidence_contract=evidence_contract,
            scoring_projection=scoring_projection,
            authority_policy=AuthorityPolicy(authority_floor=self.config.authority_floor),
            metadata={"prior_ledger_count": len(prior_ledgers), "compiler": "deterministic_fallback"},
        )
        frozen = finalize_oracle_package(package)
        if self.config.qa_required:
            assert_oracle_qa_passes(frozen)
        return frozen

    def _validation_intent(self, goal_spec: GoalSpec) -> ValidationIntent:
        text = " ".join([goal_spec.normalized_goal, *goal_spec.goal_keywords, *goal_spec.goal_phrases]).lower()
        task_classes: list[str] = []
        if any(token in text for token in ("repo", "patch", "code", "test", "file")):
            task_classes.append("repo_patch")
        if any(token in text for token in ("service", "api", "tool", "workflow")):
            task_classes.append("stateful_service")
        if any(token in text for token in ("trade", "trading", "stock", "portfolio", "alpha", "pnl")):
            task_classes.append("trading_outcome")
        if any(token in text for token in ("factual", "citation", "source", "research")):
            task_classes.append("factual_grounded")
        if not task_classes:
            task_classes.append("schema_artifact")
        return ValidationIntent(
            task_classes=task_classes,
            required_capabilities=list(goal_spec.required_capabilities or goal_spec.target_families),
            user_weights={criterion: 1.0 for criterion in goal_spec.success_criteria},
            hard_failures=list(goal_spec.constraints.get("hard_failures", [])) if isinstance(goal_spec.constraints, dict) else [],
            acceptable_tradeoffs=list(goal_spec.constraints.get("acceptable_tradeoffs", [])) if isinstance(goal_spec.constraints, dict) else [],
            authority_floor=self.config.authority_floor,
        )

    def _claim_specs(self, goal_spec: GoalSpec, intent: ValidationIntent) -> list[ClaimSpec]:
        criteria = list(goal_spec.success_criteria) or [goal_spec.normalized_goal]
        claims: list[ClaimSpec] = []
        for idx, criterion in enumerate(criteria):
            claims.append(
                ClaimSpec(
                    claim_id=f"claim.{idx+1}.{stable_hash(goal_spec.goal_id, criterion)[:8]}",
                    text=str(criterion),
                    claim_type="outcome",
                    criticality="major" if idx else "hard",
                    weight=float(intent.user_weights.get(str(criterion), 1.0)),
                    minimum_authority=intent.authority_floor,
                )
            )
        if not claims:
            claims.append(
                ClaimSpec(
                    claim_id=f"claim.goal.{stable_hash(goal_spec.goal_id)[:8]}",
                    text=goal_spec.normalized_goal,
                    criticality="hard",
                    minimum_authority=intent.authority_floor,
                )
            )
        return claims

    def _evidence_contract(self, goal_spec: GoalSpec, intent: ValidationIntent, claims: Sequence[ClaimSpec]) -> DomainEvidenceContract:
        return DomainEvidenceContract(
            contract_id=f"oracle-contract.{stable_hash(goal_spec.goal_id, intent.task_classes)[:12]}",
            domain_kind="generated_tool_workflow" if "stateful_service" not in intent.task_classes else "stateful_service",
            version="oracle-package-v1",
            scope=EvidenceScope(
                domain="oracle_package",
                domain_name=goal_spec.goal_id,
                axis_ids=[claim.claim_id for claim in claims],
                claim=goal_spec.normalized_goal,
            ),
            challenge_distribution={"task_classes": list(intent.task_classes), "minimum_pairs": 1},
            answer_mechanism={"type": "oracle_package_validators", "authority_floor": intent.authority_floor},
            quality_axes=[
                QualityAxisSpec(
                    axis_id=claim.claim_id,
                    description=claim.text,
                    weight=claim.weight,
                    minimum_authority=claim.minimum_authority,
                    comparator_type="exact_outcome",
                )
                for claim in claims
            ],
            health_floors={"validator": "pass", "leakage": "pass"},
            leakage_policy={"status_required": True, "public_projection_required": True},
            statistical_rule={"minimum_pairs": 1},
        )


__all__ = ["OracleCompiler", "OracleCompilerConfig"]
