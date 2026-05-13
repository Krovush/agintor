from __future__ import annotations
from pathlib import Path
import textwrap
ROOT = Path('/mnt/data/agintor_full_plan_patch/new_files')
files = {}
def add(path, content): files[path]=textwrap.dedent(content).lstrip()

add('agintor/oracle/validator_registry.py', r'''
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from ..contracts import ClaimSpec, ValidationIntent, ValidatorSpec

ValidatorBuilder = Callable[[ValidationIntent, Sequence[ClaimSpec]], list[ValidatorSpec]]
Applicability = Callable[[ValidationIntent, Sequence[ClaimSpec]], float]


@dataclass(frozen=True)
class ValidatorFamily:
    family_id: str
    description: str
    authority_ceiling: str = "A4"
    visibility: str = "sealed"
    leakage_risk: str = "low"
    failure_action: str = "abstain"
    can_handle: Applicability = lambda intent, claims: 0.0
    build_specs: ValidatorBuilder = lambda intent, claims: []
    health_tests: tuple[str, ...] = ()


@dataclass
class ValidatorRegistry:
    families: dict[str, ValidatorFamily] = field(default_factory=dict)

    def register(self, family: ValidatorFamily) -> None:
        if not family.family_id.strip():
            raise ValueError("validator family id may not be empty")
        self.families[family.family_id] = family

    def get(self, family_id: str) -> ValidatorFamily:
        try:
            return self.families[family_id]
        except KeyError as exc:
            raise KeyError(f"validator family {family_id!r} is not registered") from exc

    def applicable_families(
        self,
        intent: ValidationIntent,
        claims: Sequence[ClaimSpec],
        *,
        min_score: float = 0.2,
    ) -> list[tuple[float, ValidatorFamily]]:
        scored = []
        for family in self.families.values():
            try:
                score = float(family.can_handle(intent, claims))
            except Exception:
                score = 0.0
            if score >= min_score:
                scored.append((score, family))
        return sorted(scored, key=lambda item: (item[0], item[1].family_id), reverse=True)

    def build_validator_specs(
        self,
        intent: ValidationIntent,
        claims: Sequence[ClaimSpec],
        *,
        family_ids: Sequence[str] | None = None,
    ) -> list[ValidatorSpec]:
        families: Iterable[ValidatorFamily]
        if family_ids:
            families = [self.get(family_id) for family_id in family_ids]
        else:
            families = [family for _, family in self.applicable_families(intent, claims)]
        specs: list[ValidatorSpec] = []
        seen: set[str] = set()
        for family in families:
            for spec in family.build_specs(intent, claims):
                if spec.validator_id in seen:
                    continue
                seen.add(spec.validator_id)
                specs.append(spec)
        return specs


def default_validator_registry() -> ValidatorRegistry:
    registry = ValidatorRegistry()
    from .families.exact_private_answer import make_family as exact_private_answer
    from .families.schema_artifact import make_family as schema_artifact
    from .families.repo_patch import make_family as repo_patch
    from .families.stateful_service import make_family as stateful_service
    from .families.trace_state import make_family as trace_state
    from .families.factual_grounded import make_family as factual_grounded
    from .families.pairwise_preference import make_family as pairwise_preference
    from .families.trading_outcome import make_family as trading_outcome
    from .families.human_audit import make_family as human_audit
    from .families.inspect_runner import make_family as inspect_runner
    from .families.openai_eval_runner import make_family as openai_eval_runner

    for factory in (
        exact_private_answer,
        schema_artifact,
        repo_patch,
        stateful_service,
        trace_state,
        factual_grounded,
        pairwise_preference,
        trading_outcome,
        human_audit,
        inspect_runner,
        openai_eval_runner,
    ):
        registry.register(factory())
    return registry


__all__ = ["ValidatorFamily", "ValidatorRegistry", "default_validator_registry"]
''')

add('agintor/oracle/compiler.py', r'''
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
''')

add('agintor/oracle/compiler_graph.py', r'''
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..contracts import GoalSpec, OraclePackage, RuntimeSpec
from .compiler import OracleCompiler

CompilerStep = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class OracleCompilerGraph:
    """Small pluggable graph facade for the compiler workflow.

    This file intentionally does not require LangGraph at import time. When the
    dependency is present, callers can replace these steps with StateGraph nodes;
    the state contract remains the same.
    """

    compiler: OracleCompiler = field(default_factory=OracleCompiler)
    steps: list[tuple[str, CompilerStep]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.steps:
            self.steps = [
                ("goal_interpreter", self._identity),
                ("runtime_context_reader", self._identity),
                ("task_class_inferencer", self._identity),
                ("claim_decomposer", self._identity),
                ("validator_family_router", self._identity),
                ("benchmark_designer", self._identity),
                ("fixture_and_evaluator_designer", self._identity),
                ("authority_and_abstention_designer", self._identity),
                ("package_writer", self._package_writer),
                ("critic", self._identity),
                ("deterministic_qa_runner", self._identity),
                ("freeze_or_abstain", self._identity),
            ]

    @staticmethod
    def _identity(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def _package_writer(self, state: dict[str, Any]) -> dict[str, Any]:
        goal_spec = state["goal_spec"]
        runtime_spec = state.get("runtime_spec")
        state["oracle_package"] = self.compiler.compile(goal_spec, runtime_spec)
        return state

    def invoke(self, *, goal_spec: GoalSpec, runtime_spec: RuntimeSpec | None = None) -> OraclePackage:
        state: dict[str, Any] = {"goal_spec": goal_spec, "runtime_spec": runtime_spec}
        for name, step in self.steps:
            state["current_step"] = name
            state = step(state)
        return state["oracle_package"]


__all__ = ["OracleCompilerGraph"]
''')

add('agintor/oracle/subagents.py', r'''
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CompilerProposal:
    proposer_id: str
    proposal_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    confidence: float = 0.0


class OracleCompilerSubagent(Protocol):
    subagent_id: str

    def propose(self, state: dict[str, Any]) -> CompilerProposal: ...


@dataclass
class StaticSubagent:
    subagent_id: str
    proposal_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = "static proposal"
    confidence: float = 0.5

    def propose(self, state: dict[str, Any]) -> CompilerProposal:
        return CompilerProposal(
            proposer_id=self.subagent_id,
            proposal_type=self.proposal_type,
            payload=dict(self.payload),
            rationale=self.rationale,
            confidence=self.confidence,
        )


__all__ = ["CompilerProposal", "OracleCompilerSubagent", "StaticSubagent"]
''')

for path, content in files.items():
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
print(f'wrote {len(files)} files')
