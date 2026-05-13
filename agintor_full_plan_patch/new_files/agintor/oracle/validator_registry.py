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
